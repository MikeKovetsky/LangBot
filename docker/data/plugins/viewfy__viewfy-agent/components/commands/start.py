from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncGenerator

from langbot_plugin.api.definition.components.command.command import Command
from langbot_plugin.api.entities.builtin.command.context import CommandReturn, ExecuteContext

_root = str(Path(__file__).resolve().parents[2])
if _root not in sys.path:
    sys.path.insert(0, _root)
import i18n  # noqa: E402


class StartCommand(Command):
    async def initialize(self):
        await super().initialize()

        @self.subcommand(
            name="",
            help="Connect Viewfy",
            usage="start",
            aliases=[],
        )
        async def send(self, context: ExecuteContext) -> AsyncGenerator[CommandReturn, None]:
            tg_id = str(context.session.sender_id or context.session.launcher_id)
            chat_id = str(context.session.launcher_id)
            lang = self.plugin.lang_for(tg_id)
            url = None
            try:
                text, url = await self.plugin.start_reply(tg_id, lang)
            except Exception as e:
                text = i18n.prepare_fail(lang, str(e))

            sent = await self.plugin.send_connect_message(chat_id, text, url, lang=lang)
            await self.plugin.ingest(
                telegram_user_id=tg_id,
                telegram_chat_id=chat_id,
                direction="out",
                text=text,
                meta={"command": "start", "via": "command", "url": url, "lang": lang},
            )
            if sent:
                # Already posted with the inline button; avoid a second plain copy.
                return
            yield CommandReturn(text=text if not url else f"{text}\n\n{url}")
