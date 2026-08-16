from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from langbot_plugin.api.definition.components.tool.tool import Tool
from langbot_plugin.api.entities.builtin.provider import session as provider_session

_root = str(Path(__file__).resolve().parents[2])
if _root not in sys.path:
    sys.path.insert(0, _root)
import tool_call  # noqa: E402

TOOL_NAME = "scout_queue"


class ScoutQueueTool(Tool):
    async def call(
        self,
        params: dict[str, Any],
        session: provider_session.Session,
        query_id: int,
    ) -> str:
        return await tool_call.agent(self.plugin, TOOL_NAME, params, session)
