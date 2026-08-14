#!/bin/sh
# Start the plugin runtime beside LangBot and hand the container to LangBot.
#
# Order matters only for the first few seconds: LangBot retries the control
# connection, so a runtime that is still binding is not fatal. It exits with
# LangBot either way, because a container with a live runtime and a dead bot
# looks healthy to Railway while answering nobody.
set -e

# langbot_plugin validates the directory name against the manifest identity
# (author__name), so the old viewfy_agent name no longer launches. Carry the
# volume state (data/user_lang.json, settings.json) across the rename once.
PLUGIN_DIR=/app/data/plugins/viewfy__viewfy-agent
LEGACY_DIR=/app/data/plugins/viewfy_agent

if [ -d "$LEGACY_DIR" ] && [ ! -d "$PLUGIN_DIR" ]; then
  mv "$LEGACY_DIR" "$PLUGIN_DIR"
fi
rm -rf "$LEGACY_DIR"

mkdir -p "$PLUGIN_DIR"

# Code comes from the image; the plugin's own state does not. data/ holds
# user_lang.json, which a founder built up one message at a time, and settings.json
# is written below from env - a blanket copy would clobber both on every deploy.
find /app/plugin-src/viewfy__viewfy-agent -mindepth 1 -maxdepth 1 \
  ! -name data ! -name settings.json \
  -exec cp -R {} "$PLUGIN_DIR"/ \;
mkdir -p "$PLUGIN_DIR/data"

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
