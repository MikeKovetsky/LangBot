"""Diegetic Telegram offers: facts from Viewfy → LLM rewrite → append URL."""
from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from main import ViewfyAgentPlugin

log = logging.getLogger("viewfy_agent.diegetic")

OFFER_SYSTEM = """You are Viewfy, chatting with a young YC founder over Telegram.

Voice: peer founder, not coach or corporate SaaS. Short, direct, slightly sharp.
Casual but not cringe. No hype adjectives. No LinkedIn energy.

You are rewriting INTERNAL facts into a short chat offer (invite to a product, or Connect account).
Rules:
- 1-3 short lines. First line is the point. No preamble. No "Great news!".
- Diegetic only: never show UUIDs, internal ids, status enums, or JSON keys.
- Talk in product names and domains when present.
- Do NOT invent or paraphrase any URL. Do not include a link (the sender appends it).
- Never use em dashes or en dashes. Use comma, period, colon, or ASCII hyphen (-).
- Output ONLY the chat text. No quotes, no markdown fences, no labels.
"""


async def rewrite_offer(
    plugin: ViewfyAgentPlugin,
    *,
    kind: str,
    facts: dict[str, Any],
) -> str:
    """Rewrite facts to chat copy. URL is stripped from the prompt and appended after."""
    from langbot_plugin.api.entities.builtin.provider import message as provider_message

    url = (facts.get("url") or "").strip()
    safe = {k: v for k, v in facts.items() if k != "url"}
    model = (plugin.llm_model_uuid or "").strip()
    if not model:
        # Minimal fallback without hardcoded product marketing — still append URL.
        name = facts.get("product_name") or "the product"
        if kind == "product_invite":
            text = f"You're in the group for {name}. Tap the link to join and link Telegram."
        else:
            text = "Link Telegram to Viewfy to continue."
        return _with_url(text, url)

    msg = await plugin.invoke_llm(
        llm_model_uuid=model,
        messages=[
            provider_message.Message(role="system", content=OFFER_SYSTEM),
            provider_message.Message(
                role="user",
                content=(
                    "Rewrite these facts into a short diegetic Telegram offer.\n"
                    f"kind: {kind}\n"
                    f"facts_json: {json.dumps(safe, ensure_ascii=False)}"
                ),
            ),
        ],
        extra_args={"temperature": 0.6, "max_tokens": 180},
    )
    text = (msg.content or "").strip() if hasattr(msg, "content") else str(msg).strip()
    if isinstance(text, list):
        parts = []
        for p in text:
            if isinstance(p, dict) and p.get("text"):
                parts.append(str(p["text"]))
            else:
                parts.append(str(getattr(p, "text", p)))
        text = "".join(parts).strip()
    if not text:
        raise RuntimeError("empty LLM rewrite")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    # Strip any hallucinated URL lines; we append the real one.
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith(("http://", "https://"))]
    text = "\n".join(lines).strip()[:800]
    return _with_url(text, url)


def _with_url(text: str, url: str) -> str:
    text = (text or "").strip()
    url = (url or "").strip()
    if not url:
        return text
    if url in text:
        return text
    return f"{text}\n\n{url}" if text else url


async def mint_product_invite_facts(
    plugin: ViewfyAgentPlugin,
    *,
    telegram_user_id: str,
    telegram_chat_id: str,
) -> dict[str, Any]:
    out = await plugin._request(
        "POST",
        "/api/telegram/agent/product_invite",
        body={
            "telegram_user_id": str(telegram_user_id),
            "telegram_chat_id": str(telegram_chat_id),
        },
    )
    if out.get("_http_status") or not out.get("url"):
        raise RuntimeError(out.get("detail") or "product_invite mint failed")
    return {
        "url": str(out["url"]),
        "expires_in": out.get("expires_in"),
        "product_name": out.get("product_name") or "",
        "product_domain": out.get("product_domain") or "",
        "product_id": out.get("product_id"),
        "invited_telegram_user_id": out.get("invited_telegram_user_id"),
    }


async def prepare_connect_facts(
    plugin: ViewfyAgentPlugin,
    *,
    telegram_user_id: str,
    lang: str | None = None,
) -> dict[str, Any]:
    url = await plugin.prepare_link(telegram_user_id, lang)
    return {"url": url, "outcome": "connect_offer", "lang": lang or "en"}
