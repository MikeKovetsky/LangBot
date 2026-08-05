# LangBot on Railway

Moves the Telegram bot off a laptop. The Viewfy backend already runs on Railway;
until this, `@viewfy_ai_bot` long-polled from local Docker, so closing the lid took
the product down while the API stayed healthy.

## Shape

One service, not two. Compose runs `langbot` and `langbot_plugin_runtime` as
separate containers because it can bind-mount the plugin into both. Railway has no
host to mount from and bills per service, so `entrypoint.sh` starts the runtime
beside LangBot and they talk over `ws://localhost:5400` — the transport LangBot
already uses on Windows.

- **Patched core files** are baked in (compose mounts them; keep the two in step).
- **The plugin** is baked to `/app/plugin-src` and copied to `/app/data/plugins/…`
  on boot, because the volume mounts over `/app/data` and would shadow it. The copy
  is file-by-file, so `data/user_lang.json` survives a deploy.
- **`settings.json`** is written from env at boot. It is gitignored and must never
  be baked into the image.

## Service settings

| Setting | Value |
|---|---|
| Build | Dockerfile `docker/railway/Dockerfile`, context = repo root |
| Volume | mount at `/app/data` |
| Port | 5300 (dashboard) — only if you want the web UI exposed |

Environment:

| Var | Notes |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token, same one the local stack used |
| `VIEWFY_TELEGRAM_AGENT_SECRET` | matches the backend's `telegram_agent_secret` |
| `VIEWFY_API_BASE` | `https://api.viewfy.ai` |
| `VIEWFY_BOT_UUID` / `VIEWFY_LLM_MODEL_UUID` | from the LangBot dashboard; the outbox poller stays off without them |
| `LANGBOT_PLUGIN_RUNTIME_CONTROL_TOKEN` | `openssl rand -hex 32`; one process pair, but the channel still authenticates |
| `TZ` | `Asia/Shanghai` locally — pick deliberately, it is what log timestamps read in |

## Data

`docker/data/langbot.db` holds the bot registration, pipeline config (group wake
rules, prompts), model providers, and conversation history. It is untracked, so it
exists only on the laptop. Copy it into the volume before first boot, or the bot
starts as a fresh install with nothing configured.

## Cutover

Telegram allows exactly one `getUpdates` consumer per token. Two long-polls fight
and both drop messages, so this is strictly ordered:

1. `docker compose stop langbot` locally.
2. Deploy on Railway, watch for `Connected to plugin runtime` and `register_plugin`.
3. Send a message; confirm a reply.

Rollback is the same in reverse: scale the Railway service to zero, then start the
local container. Never both.

## Open

- Box retries `ws://langbot_box:5410` twice at boot despite `BOX__ENABLED=false`.
  Harmless, but confirm it does not settle into a 60s retry loop in Railway's logs.
