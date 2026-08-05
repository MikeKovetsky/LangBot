#!/bin/sh
# Start the plugin runtime beside LangBot and hand the container to LangBot.
#
# Order matters only for the first few seconds: LangBot retries the control
# connection, so a runtime that is still binding is not fatal. It exits with
# LangBot either way, because a container with a live runtime and a dead bot
# looks healthy to Railway while answering nobody.
set -e

PLUGIN_DIR=/app/data/plugins/viewfy_agent

mkdir -p "$PLUGIN_DIR"

# Code comes from the image; the plugin's own state does not. Copying file by
# file leaves data/ and settings.json alone, so a deploy never drops a founder's
# language preference.
cp -R /app/plugin-src/viewfy_agent/. "$PLUGIN_DIR"/

# Secrets live in Railway's environment, never in the image or the repo.
if [ -n "$VIEWFY_TELEGRAM_AGENT_SECRET" ]; then
  cat > "$PLUGIN_DIR/settings.json" <<JSON
{
  "viewfy_api_base": "${VIEWFY_API_BASE:-https://api.viewfy.ai}",
  "telegram_agent_secret": "${VIEWFY_TELEGRAM_AGENT_SECRET}",
  "telegram_bot_token": "${TELEGRAM_BOT_TOKEN}",
  "sticker_set": "${VIEWFY_STICKER_SET:-viewfy}",
  "bot_uuid": "${VIEWFY_BOT_UUID}",
  "llm_model_uuid": "${VIEWFY_LLM_MODEL_UUID}"
}
JSON
else
  echo "railway-entrypoint: VIEWFY_TELEGRAM_AGENT_SECRET unset, keeping existing settings.json" >&2
fi

uv run --no-sync -m langbot_plugin.cli.__init__ rt &
RUNTIME_PID=$!

# Do not outlive LangBot.
trap 'kill "$RUNTIME_PID" 2>/dev/null' EXIT INT TERM

exec uv run --no-sync main.py
