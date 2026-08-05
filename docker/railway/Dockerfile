# LangBot for Railway: one service running both processes.
#
# Locally, compose runs langbot and langbot_plugin_runtime as two containers on
# a shared network, with the patched core files and the plugin bind-mounted from
# the host. Railway has no host to mount from and bills per service, so here the
# patches and the plugin are baked in and the runtime is started beside LangBot,
# reached over localhost — the same transport LangBot already uses on Windows.
FROM rockchin/langbot:latest

# Dogfood patches. Kept in step with docker-compose.yaml, which mounts these
# same three files; a base image bump means re-diffing them.
COPY src/langbot/pkg/platform/sources/telegram.py /app/src/langbot/pkg/platform/sources/telegram.py
COPY src/langbot/pkg/provider/modelmgr/requesters/litellmchat.py /app/src/langbot/pkg/provider/modelmgr/requesters/litellmchat.py
COPY src/langbot/pkg/pipeline/wrapper/wrapper.py /app/src/langbot/pkg/pipeline/wrapper/wrapper.py

# The plugin ships in the image, not the volume: /app/data is the Railway volume
# mount and would shadow anything baked underneath it. The entrypoint syncs this
# into place on every boot, so a deploy updates the code.
COPY docker/data/plugins/viewfy_agent /app/plugin-src/viewfy_agent

COPY docker/railway/entrypoint.sh /app/railway-entrypoint.sh
RUN chmod +x /app/railway-entrypoint.sh

# Both processes in one container, so the control channel never leaves it.
ENV PLUGIN__RUNTIME_WS_URL=ws://localhost:5400/control/ws

# Off, as in compose. Box only adds sandbox tools and stdio MCP, which nothing
# here uses, and when enabled LangBot retries ws://langbot_box:5410 every 60s.
ENV BOX__ENABLED=false

CMD ["/app/railway-entrypoint.sh"]
