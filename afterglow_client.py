"""Afterglow OpenAI 兼容 API 客户端（仅 chat completions 子集）。

职责（SRP）：把"用户文本 + 会话标识"翻译成对 Afterglow `/v1/chat/completions`
的一次调用，并返回应当回复的文本；若 Afterglow 判定本轮应当沉默则返回 None。

会话历史（辅助上下文）：
  Afterglow 后端虽然通过 `conversation_id` 在 LanceDB 维护 live memory，
  但 OpenAI 协议本身的 messages 数组也是后端 prompt 的直接输入。客户端在
  内存里保留每个会话的最近 N 轮 user/assistant 消息一起送给后端，能让
  后端无需检索就拿到当前轮上下文，对短期连贯性更准。

  - 沉默轮（policy/finish_reason/sentinel 命中）不写入历史，避免下一轮把
    "[silent]" 当成真实回复
  - 调用失败（抛 AfterglowError）也不写入历史，避免污染后续上下文
  - per-conversation `asyncio.Lock` 保证同一会话的请求严格按顺序更新历史
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger("afterglow.client")

# 默认沉默 sentinel（与 Afterglow 后端 SILENCE_RESPONSE_SENTINEL 默认值一致）
DEFAULT_SILENCE_SENTINEL = "[silent]"

# 默认保留的最近轮数（一轮 = 一条 user + 一条 assistant）
DEFAULT_HISTORY_MAX_TURNS = 6


class AfterglowError(RuntimeError):
    """Afterglow 调用层异常。"""


class AfterglowClient:
    """单实例长连接客户端（asyncio）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        model: str = "afterglow",
        timeout: float = 120.0,
        silence_sentinel: str = DEFAULT_SILENCE_SENTINEL,
        history_max_turns: int = DEFAULT_HISTORY_MAX_TURNS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._silence_sentinel = silence_sentinel
        # 一轮 = 2 条消息（user + assistant）；0 表示不携带历史
        self._history_max_messages = max(history_max_turns, 0) * 2
        # 聊天接口可能较慢（检索 + 主模型生成），timeout 默认放宽到 120s
        self._client = httpx.AsyncClient(timeout=timeout)

        # 每个 conversation_id 一份历史 + 一把锁，保证同会话顺序更新
        self._histories: dict[str, list[dict[str, str]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # --------------------------------------------------------------- history

    def _get_lock(self, conversation_id: str) -> asyncio.Lock:
        """惰性创建 per-conversation 锁。setdefault 在单线程 asyncio 下原子。"""
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    def reset_history(self, conversation_id: str) -> None:
        """清空指定会话的客户端侧历史（不影响后端 live memory）。"""
        self._histories.pop(conversation_id, None)

    # ---------------------------------------------------------------- chat

    async def chat(self, *, conversation_id: str, user_text: str) -> str | None:
        """单轮请求 Afterglow，返回需要发给用户的文本。

        :param conversation_id: 稳定标识（同一 QQ 用户复用同一 ID，让后端维护记忆）
        :param user_text: 用户原文
        :return: 回复文本；若判定沉默则返回 None
        :raises AfterglowError: 网络/鉴权/协议错误
        """
        async with self._get_lock(conversation_id):
            history = self._histories.get(conversation_id, [])
            # OpenAI 标准 messages：历史 + 当前 user 消息
            messages: list[dict[str, str]] = [
                *history,
                {"role": "user", "content": user_text},
            ]

            payload: dict[str, Any] = {
                # model 字段 Afterglow 视为占位（实际用 .env 配的 CHAT_MODEL），但传一下兼容性更好
                "model": self._model,
                "messages": messages,
                "stream": False,
                "conversation_id": conversation_id,
            }
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            url = f"{self._base_url}/v1/chat/completions"

            try:
                resp = await self._client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise AfterglowError(f"请求 Afterglow 失败：{exc}") from exc

            if resp.status_code >= 400:
                raise AfterglowError(
                    f"Afterglow 返回 {resp.status_code}：{resp.text[:500]}"
                )

            try:
                data = resp.json()
            except ValueError as exc:
                raise AfterglowError(f"Afterglow 响应非 JSON：{resp.text[:200]}") from exc

            reply = self._extract_reply(data)

            # 仅在非沉默 / 非失败时把这一轮追加到历史
            # 失败已通过抛异常短路，这里只需检查沉默
            if reply is not None:
                self._append_history(conversation_id, user_text, reply)

            return reply

    def _append_history(
        self, conversation_id: str, user_text: str, assistant_text: str
    ) -> None:
        """追加一轮对话，并按上限裁尾。"""
        history = self._histories.get(conversation_id, [])
        history = [
            *history,
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ]
        if self._history_max_messages > 0 and len(history) > self._history_max_messages:
            history = history[-self._history_max_messages:]
        self._histories[conversation_id] = history

    # --------------------------------------------------------------- parse

    def _extract_reply(self, data: dict[str, Any]) -> str | None:
        """从 OpenAI 兼容响应中提取回复，识别 Afterglow 的沉默信号。

        沉默判断三选一（任一命中即视为沉默）：
          1. `policy.should_reply == false`（Afterglow 扩展字段，最权威）
          2. `choices[0].finish_reason == "silenced"`
          3. content 文本等于 sentinel（默认 "[silent]"）
        """
        # 1. policy 顶层字段（最准确）
        policy = data.get("policy") or {}
        if policy.get("should_reply") is False:
            logger.info("Afterglow 决策沉默：%s", policy.get("reason") or policy)
            return None

        choices = data.get("choices") or []
        if not choices:
            raise AfterglowError(f"Afterglow 响应缺少 choices：{data}")

        choice = choices[0] or {}
        finish_reason = choice.get("finish_reason")
        message = choice.get("message") or {}
        content = message.get("content")

        # content 可能是 list（多模态），简单兜底
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        content = (content or "").strip()

        # 2. finish_reason
        if finish_reason == "silenced":
            logger.info("Afterglow finish_reason=silenced，跳过回复")
            return None

        # 3. sentinel 字符串
        if content == self._silence_sentinel:
            logger.info("Afterglow 返回 sentinel %r，跳过回复", self._silence_sentinel)
            return None

        if not content:
            # 非沉默但空内容：当作异常上抛，避免静默丢消息
            raise AfterglowError(f"Afterglow 返回空内容：{data}")

        return content
