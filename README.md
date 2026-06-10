# Afterglow QQ Bot

把 QQ 私聊接到本地 [Afterglow](https://github.com/kldhsh123/Afterglow) 后端：用户在 QQ 上私聊机器人，机器人把消息转给 Afterglow 的 `/v1/chat/completions`，再把回复发回 QQ。

每个 QQ 用户在 Afterglow 后端视为一条独立会话（`conversation_id = qq:{user_openid}`）。客户端在本地 SQLite 文件里维护每个会话的最近 N 轮 user/assistant 消息，作为辅助上下文随请求一并发给后端；长期记忆仍由 Afterglow 后端通过 LanceDB 持久化。

如果配置了聊天历史压缩，客户端会在本地历史达到指定轮数或估算 token 阈值时，通过 OpenAI Chat Completions 兼容接口生成摘要，并保留最近几轮原文继续对话。

未启用压缩时，`AFTERGLOW_HISTORY_MAX_TURNS` 是本地短期历史硬上限；启用压缩后，原始历史会保留到压缩阈值，再替换为"摘要 + 最近 N 轮原文"。

---

## 项目结构

```
Afterglow-QQBot/
├── qqbot/                  
│   ├── __init__.py
│   ├── api.py              # REST：token / 发文本 / 发图
│   ├── gateway.py          # WebSocket：Hello/Identify/Heartbeat/Resume
│   └── schedule_tasks.py   # Afterglow schedule_tasks → 本地 QQ 定时消息
├── afterglow_client.py     # Afterglow OpenAI 兼容客户端（SRP，仅 chat completions）
├── main.py                 # 粘合层：C2C 消息 → Afterglow → 回复
├── .env.example            # 配置模板
└── requirements.txt        # httpx + websockets
```

---

## 快速开始

### 1. 准备 Afterglow 后端

先把 [Afterglow](https://github.com/kldhsh123/Afterglow) 后端跑起来，确认 `XUWEN_API_KEY` 已设置：

```bash
cd /path/to/Afterglow/backend
uv run uvicorn xuwen.chat_api.app:create_app --factory
# → http://127.0.0.1:8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/healthz
```

### 2. 准备 QQ 机器人凭据

在 [QQ 开放平台](https://q.qq.com) 创建OpenClaw机器人应用，记下 `AppID` 和 `Secret`

### 3. 配置 `.env`

```bash
cp .env.example .env
# 编辑 .env，填入：
#   QQ_BOT_APP_ID / QQ_BOT_CLIENT_SECRET
#   AFTERGLOW_API_KEY  ← 与 Afterglow backend/.env 中的 XUWEN_API_KEY 一致
```

### 4. 安装依赖并启动

```bash
pip install -r requirements.txt
python main.py
```

启动后用 QQ 私聊你的机器人，第一条消息会触发 Afterglow 检索 + 生成（可能 5–30 秒），随后正常对话。

---

## 配置项

| 变量 | 必需 | 默认 | 说明 |
|---|---|---|---|
| `QQ_BOT_APP_ID` | ✅ | — | QQ 开放平台 AppID |
| `QQ_BOT_CLIENT_SECRET` | ✅ | — | QQ 开放平台 Secret |
| `AFTERGLOW_BASE_URL` | ✅ | `http://127.0.0.1:8000` | Afterglow 后端地址 |
| `AFTERGLOW_API_KEY` | ✅ | — | Afterglow 的 `XUWEN_API_KEY` |
| `AFTERGLOW_MODEL` | ❌ | `afterglow` | 占位字段，后端实际用 `.env` 的 `CHAT_MODEL` |
| `AFTERGLOW_TIMEOUT` | ❌ | `120` | 单次请求超时（秒） |
| `AFTERGLOW_SILENCE_SENTINEL` | ❌ | `[silent]` | 与后端 `SILENCE_RESPONSE_SENTINEL` 保持一致 |
| `AFTERGLOW_HISTORY_MAX_TURNS` | ❌ | `6` | 客户端在 messages 数组里携带的最近轮数（一轮 = user+assistant）；`0` = 不携带 |
| `AFTERGLOW_HISTORY_DB_PATH` | ❌ | `data/chat_history.sqlite3` | 本地短期聊天记录 SQLite 路径；相对路径按项目目录解析 |
| `AFTERGLOW_HISTORY_COMPRESSION_API_KEY` | ❌ | （空） | 历史压缩使用的 OpenAI-compatible API key；留空禁用压缩 |
| `AFTERGLOW_HISTORY_COMPRESSION_BASE_URL` | ❌ | `https://api.openai.com` | 历史压缩 API 地址，支持 `/v1/chat/completions` 兼容服务 |
| `AFTERGLOW_HISTORY_COMPRESSION_MODEL` | ❌ | （空） | 历史压缩模型；留空禁用压缩 |
| `AFTERGLOW_HISTORY_COMPRESSION_TRIGGER_TURNS` | ❌ | `0` | 达到多少轮 user+assistant 后压缩；`0` = 不按轮数触发 |
| `AFTERGLOW_HISTORY_COMPRESSION_TRIGGER_TOKENS` | ❌ | `0` | 达到多少估算 token 后压缩；`0` = 不按 token 触发 |
| `AFTERGLOW_HISTORY_COMPRESSION_KEEP_TURNS` | ❌ | `3` | 每次压缩后保留最近多少轮原文 |
| `AFTERGLOW_HISTORY_COMPRESSION_TIMEOUT` | ❌ | `60` | 历史压缩 API 请求超时（秒） |
| `AFTERGLOW_HISTORY_COMPRESSION_MAX_OUTPUT_TOKENS` | ❌ | `800` | 摘要最大输出 token |
| `AFTERGLOW_SPLIT_ASSISTANT_MESSAGES` | ❌ | `true` | 按 assistant `content` 里的双换行拆成多条 QQ 气泡 |
| `AFTERGLOW_MESSAGE_SEGMENT_MIN_DELAY` | ❌ | `1.5` | 多气泡分条时的最小段间延迟（秒） |
| `AFTERGLOW_MESSAGE_SEGMENT_MAX_DELAY` | ❌ | `3.0` | 多气泡分条时的最大段间延迟（秒） |
| `AFTERGLOW_SCHEDULE_TASKS_ENABLED` | ❌ | `true` | 是否消费 Afterglow 顶层 `schedule_tasks` 并注册 QQ 定时消息 |
| `AFTERGLOW_SCHEDULE_DB_PATH` | ❌ | `data/schedule_tasks.sqlite3` | 本地定时任务 SQLite 路径；相对路径按项目目录解析 |
| `AFTERGLOW_SCHEDULE_POLL_INTERVAL` | ❌ | `1.0` | 本地定时任务轮询间隔（秒） |
| `AFTERGLOW_ERROR_REPLY` | ❌ | （空） | 调用失败时的兜底回复；留空则不回（更接近真人"没动静"） |
| `AFTERGLOW_ALLOWED_OPENIDS` | ❌ | （空） | 逗号分隔的 user_openid 白名单；留空 = 任何人都能触发（启动时会 warning） |
| `AFTERGLOW_DENIED_REPLY` | ❌ | （空） | 非白名单用户的回复；留空 = 静默丢弃（不暴露机器人存在） |
| `LOG_LEVEL` | ❌ | `INFO` | `DEBUG` / `INFO` / `WARNING` |

---

### 用户白名单

`AFTERGLOW_ALLOWED_OPENIDS` 留空时所有 QQ 用户都能触发（启动时 logger 会 warning 提示）；填入逗号分隔的 `user_openid` 后只放行命中项。

由于 `user_openid` 是 QQ 平台分配的不透明字符串（用户本人也不知道自己的 openid），实际配置流程：

1. 先留空 `AFTERGLOW_ALLOWED_OPENIDS` 启动
2. 让目标用户给机器人私聊任意一条消息
3. 在日志里找 `收到私聊 openid=xxx` 或被拒绝时的 `拒绝非白名单消息 openid=xxx`
4. 把 `xxx` 加进 `.env` 重启

被拒绝的消息默认静默丢弃（不暴露机器人存在）。如果希望明确告诉对方"没权限"，配 `AFTERGLOW_DENIED_REPLY`。

---

## License

随 Afterglow 主项目，AGPL-3.0-or-later。
