from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncGenerator

from langbot_plugin.api.definition.components.command.command import Command
from langbot_plugin.api.entities.builtin.command.context import CommandReturn, ExecuteContext

_root = str(Path(__file__).resolve().parents[2])
if _root not in sys.path:
    sys.path.insert(0, _root)


class TzCommand(Command):
    """Telegram never sends a timezone, so the founder states it once.

    Without it every date the agent says is in whatever the server runs in, which
    is how "1 user today" ended up meaning yesterday.
    """

    async def initialize(self):
        await super().initialize()

        @self.subcommand(
            name="",
            help="Set your timezone, e.g. tz Europe/Kyiv",
            usage="tz <IANA zone>",
            aliases=[],
        )
        async def set_tz(self, context: ExecuteContext) -> AsyncGenerator[CommandReturn, None]:
            tg_id = str(context.session.sender_id or context.session.launcher_id)
            # params[0] is the command word itself when the dispatcher does not strip it.
            parts = [x for x in (context.crt_params or []) if x]
            if parts and parts[0].lower() == "tz":
                parts = parts[1:]
            arg = " ".join(parts).strip()
            now, zone = self.plugin.now_for(tg_id)

            if not arg:
                yield CommandReturn(
                    text=(
                        f"Your timezone is {zone}. Local time {now:%H:%M} on {now:%Y-%m-%d}.\n"
                        "Change it with: tz Europe/Kyiv"
                    )
                )
                return

            if not self.plugin.remember_tz(tg_id, arg):
                yield CommandReturn(
                    text=f"{arg} is not a timezone I know. Use an IANA name like Europe/Kyiv."
                )
                return

            now, zone = self.plugin.now_for(tg_id)
            yield CommandReturn(text=f"Timezone set to {zone}. Local time {now:%H:%M} on {now:%Y-%m-%d}.")
