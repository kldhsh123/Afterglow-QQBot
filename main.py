"""Afterglow QQ Bot 主入口。

把 QQ 私聊（C2C）消息转发给本地 Afterglow `/v1/chat/completions`，
并把 Afterglow 的回复发回给用户。

会话连续性：以 `user_openid` 派生 `conversation_id`，每个 QQ 用户
在 Afterglow 后端视为独立会话，记忆由后端自行通过 LanceDB 维护。

运行：
    cp .env.example .env
    # 编辑 .env 填入 QQ_BOT_* 与 AFTERGLOW_*
    python main.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import signal
import sys
from pathlib import Path

from afterglow_client import AfterglowClient, AfterglowError, HistoryCompressionConfig
from qqbot import C2CMessage, QQBotAPI, QQBotGateway
from qqbot.schedule_tasks import QQScheduleTaskRunner

logger = logging.getLogger("afterglow.qqbot")


def _load_env_from_file(env_path: Path) -> None:
    """极简 .env 加载（不依赖 python-dotenv），仅在键未设置时填入。"""
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.stderr.write(f"缺少环境变量 {name}\n")
        sys.exit(1)
    return value


def _build_conversation_id(openid: str) -> str:
    """每个 QQ 用户固定一个会话 ID，让 Afterglow 自行维护记忆。"""
    return f"qq:{openid}"


def _parse_openid_set(raw: str) -> set[str] | None:
    """解析逗号分隔的 openid 列表。空字符串返回 None（视为不启用过滤）。"""
    raw = raw.strip()
    if not raw:
        return None
    return {item.strip() for item in raw.split(",") if item.strip()}


def _parse_bool(raw: str, *, default: bool) -> bool:
    raw = raw.strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _resolve_project_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    return path


def _split_assistant_message(content: str) -> list[str]:
    """Split assistant content into QQ/WeChat-style bubbles.

    Afterglow uses two or more consecutive newlines as message separators.
    Single newlines remain inside the same message bubble.
    """
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    segments: list[str] = []
    for segment in re.split(r"\n{2,}", normalized):
        segment = segment.strip("\n")
        if segment.strip():
            segments.append(segment)
    return segments


async def _send_assistant_reply(
    api: QQBotAPI,
    *,
    openid: str,
    msg_id: str,
    content: str,
    split_messages: bool,
    segment_delay_min: float,
    segment_delay_max: float,
) -> None:
    if split_messages:
        segments = _split_assistant_message(content)
    else:
        segments = [content]

    if not segments:
        return

    for index, segment in enumerate(segments):
        if index > 0:
            await asyncio.sleep(random.uniform(segment_delay_min, segment_delay_max))
        await api.send_c2c_text(openid, segment, msg_id=msg_id)


async def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    _load_env_from_file(Path(__file__).parent / ".env")

    app_id = _require_env("QQ_BOT_APP_ID")
    client_secret = _require_env("QQ_BOT_CLIENT_SECRET")
    afterglow_base = _require_env("AFTERGLOW_BASE_URL")
    afterglow_key = _require_env("AFTERGLOW_API_KEY")
    afterglow_model = os.environ.get("AFTERGLOW_MODEL", "afterglow")
    afterglow_timeout = float(os.environ.get("AFTERGLOW_TIMEOUT", "120"))
    silence_sentinel = os.environ.get("AFTERGLOW_SILENCE_SENTINEL", "[silent]")
    history_max_turns = int(os.environ.get("AFTERGLOW_HISTORY_MAX_TURNS", "6"))
    history_db_raw = os.environ.get(
        "AFTERGLOW_HISTORY_DB_PATH",
        "data/chat_history.sqlite3",
    )
    history_db_path = _resolve_project_path(history_db_raw)
    history_compression = HistoryCompressionConfig(
        api_key=os.environ.get("AFTERGLOW_HISTORY_COMPRESSION_API_KEY", "").strip(),
        model=os.environ.get("AFTERGLOW_HISTORY_COMPRESSION_MODEL", "").strip(),
        base_url=os.environ.get(
            "AFTERGLOW_HISTORY_COMPRESSION_BASE_URL",
            "https://api.openai.com",
        ).strip(),
        trigger_turns=int(
            os.environ.get("AFTERGLOW_HISTORY_COMPRESSION_TRIGGER_TURNS", "0")
        ),
        trigger_tokens=int(
            os.environ.get("AFTERGLOW_HISTORY_COMPRESSION_TRIGGER_TOKENS", "0")
        ),
        keep_turns=int(
            os.environ.get("AFTERGLOW_HISTORY_COMPRESSION_KEEP_TURNS", "3")
        ),
        timeout=float(os.environ.get("AFTERGLOW_HISTORY_COMPRESSION_TIMEOUT", "60")),
        max_output_tokens=int(
            os.environ.get("AFTERGLOW_HISTORY_COMPRESSION_MAX_OUTPUT_TOKENS", "800")
        ),
    )
    error_reply = os.environ.get("AFTERGLOW_ERROR_REPLY", "").strip()
    allowed_openids = _parse_openid_set(
        os.environ.get("AFTERGLOW_ALLOWED_OPENIDS", "")
    )
    denied_reply = os.environ.get("AFTERGLOW_DENIED_REPLY", "").strip()
    split_messages = _parse_bool(
        os.environ.get("AFTERGLOW_SPLIT_ASSISTANT_MESSAGES", "true"),
        default=True,
    )
    segment_delay_min = float(
        os.environ.get("AFTERGLOW_MESSAGE_SEGMENT_MIN_DELAY", "1.5")
    )
    segment_delay_max = float(
        os.environ.get("AFTERGLOW_MESSAGE_SEGMENT_MAX_DELAY", "3.0")
    )
    if segment_delay_min < 0:
        segment_delay_min = 0.0
    if segment_delay_max < segment_delay_min:
        segment_delay_max = segment_delay_min
    schedule_tasks_enabled = _parse_bool(
        os.environ.get("AFTERGLOW_SCHEDULE_TASKS_ENABLED", "true"),
        default=True,
    )
    schedule_db_path = _resolve_project_path(
        os.environ.get("AFTERGLOW_SCHEDULE_DB_PATH", "data/schedule_tasks.sqlite3")
    )
    schedule_poll_interval = float(
        os.environ.get("AFTERGLOW_SCHEDULE_POLL_INTERVAL", "1.0")
    )

    if allowed_openids:
        logger.info("白名单已启用，允许 %d 个 openid", len(allowed_openids))
    else:
        logger.warning("白名单未启用，任何 QQ 用户都能触发 Afterglow")

    api = QQBotAPI(app_id=app_id, client_secret=client_secret)
    gw = QQBotGateway(api)
    afterglow = AfterglowClient(
        base_url=afterglow_base,
        api_key=afterglow_key,
        model=afterglow_model,
        timeout=afterglow_timeout,
        silence_sentinel=silence_sentinel,
        history_max_turns=history_max_turns,
        history_db_path=history_db_path,
        history_compression=history_compression,
    )
    schedule_runner: QQScheduleTaskRunner | None = None
    if schedule_tasks_enabled:
        schedule_runner = QQScheduleTaskRunner(
            api,
            schedule_db_path,
            poll_interval=schedule_poll_interval,
        )
        await schedule_runner.start()

    @gw.on_c2c_message
    async def on_c2c(msg: C2CMessage) -> None:
        # 白名单校验：未配置时放行所有；配置后只放行命中项
        if allowed_openids is not None and msg.user_openid not in allowed_openids:
            # 用 warning 级别打 openid，方便管理员从日志里把新用户加进白名单
            logger.warning(
                "拒绝非白名单消息 openid=%s（加入白名单请把它追加到 AFTERGLOW_ALLOWED_OPENIDS）",
                msg.user_openid,
            )
            if denied_reply:
                try:
                    await api.send_c2c_text(
                        msg.user_openid, denied_reply, msg_id=msg.message_id
                    )
                except Exception:
                    logger.exception("发送拒绝回复失败")
            return

        text = msg.content.strip()
        if not text:
            return  # QQ 空消息直接忽略，避免无意义请求
        logger.info("收到私聊 openid=%s len=%d", msg.user_openid, len(text))

        conversation_id = _build_conversation_id(msg.user_openid)

        try:
            reply = await afterglow.chat(
                conversation_id=conversation_id, user_text=text
            )
        except AfterglowError as exc:
            logger.error("Afterglow 调用失败：%s", exc)
            if error_reply:
                # 仅在用户配置了兜底回复时才回，避免和"沉默策略"语义冲突
                try:
                    await api.send_c2c_text(
                        msg.user_openid, error_reply, msg_id=msg.message_id
                    )
                except Exception:
                    logger.exception("发送兜底回复失败")
            return

        if reply is None:
            # Afterglow 决策沉默，不发任何消息
            return

        if schedule_runner is not None:
            await schedule_runner.add_tasks(msg.user_openid, reply.schedule_tasks)
        elif reply.schedule_tasks:
            logger.info(
                "收到 %d 条 schedule_tasks，但 AFTERGLOW_SCHEDULE_TASKS_ENABLED=false，已忽略",
                len(reply.schedule_tasks),
            )

        if not reply.content:
            return

        try:
            if reply.reply_delay_seconds > 0:
                await asyncio.sleep(reply.reply_delay_seconds)
            await _send_assistant_reply(
                api,
                openid=msg.user_openid,
                msg_id=msg.message_id,
                content=reply.content,
                split_messages=split_messages,
                segment_delay_min=segment_delay_min,
                segment_delay_max=segment_delay_max,
            )
        except Exception:
            logger.exception("发送 C2C 文本失败 openid=%s", msg.user_openid)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, gw.stop)
        except NotImplementedError:
            # Windows 不支持 add_signal_handler，由 KeyboardInterrupt 兜底
            pass

    try:
        await gw.run()
    finally:
        if schedule_runner is not None:
            await schedule_runner.stop()
        await api.aclose()
        await afterglow.aclose()

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
