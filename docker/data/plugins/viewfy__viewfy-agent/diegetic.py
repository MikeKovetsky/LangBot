"""Diegetic Telegram offers: facts from Viewfy → LLM rewrite → append URL."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from main import ViewfyAgentPlugin

log = logging.getLogger("viewfy_agent.diegetic")

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_UNTIL_CLOSE = re.compile(r"^.*?</think>", re.DOTALL | re.IGNORECASE)

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


def _hours(facts: dict[str, Any]) -> int:
    try:
        secs = int(facts.get("expires_in") or 0)
    except (TypeError, ValueError):
        secs = 0
    if secs <= 0:
        return 48
    return max(1, (secs + 3599) // 3600)


def _fallback(kind: str, facts: dict[str, Any], lang: str) -> str:
    import i18n

    lang_n = i18n.normalize_lang(lang)
    if kind == "product_invite":
        return i18n.invite_offer(
            lang_n,
            name=str(facts.get("product_name") or ""),
            domain=str(facts.get("product_domain") or ""),
            hours=_hours(facts),
        )
    return i18n.connect_offer(lang_n)


async def rewrite_offer(
    plugin: ViewfyAgentPlugin,
    *,
    kind: str,
    facts: dict[str, Any],
    lang: str = "en",
) -> str:
    """Rewrite facts to chat copy. URL is stripped from the prompt and appended after."""
    from langbot_plugin.api.entities.builtin.provider import message as provider_message
    import i18n

    url = (facts.get("url") or "").strip()
    safe = {k: v for k, v in facts.items() if k != "url"}
    lang_n = i18n.normalize_lang(lang)
    model = (plugin.llm_model_uuid or "").strip()
    if not model:
        return _with_url(_fallback(kind, facts, lang_n), url)

    system = OFFER_SYSTEM
    if lang_n != "en":
        system = system.rstrip() + "\n\n" + i18n.prompt_lang_line(lang_n)

    msg = await plugin.invoke_llm(
        llm_model_uuid=model,
        messages=[
            provider_message.Message(role="system", content=system),
            provider_message.Message(
                role="user",
                content=(
                    "Rewrite these facts into a short diegetic Telegram offer.\n"
                    f"{i18n.prompt_lang_line(lang_n)}\n"
                    f"kind: {kind}\n"
                    f"facts_json: {json.dumps(safe, ensure_ascii=False)}"
                ),
            ),
        ],
        extra_args={"max_tokens": 180},
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
        return _with_url(_wake(_fallback(kind, facts, lang_n), kind, lang_n), url)
    text = _strip_think(text)
    if not text:
        return _with_url(_wake(_fallback(kind, facts, lang_n), kind, lang_n), url)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    # Strip any hallucinated URL lines; we append the real one.
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith(("http://", "https://"))]
    text = "\n".join(lines).strip()[:800]
    if not text:
        return _with_url(_wake(_fallback(kind, facts, lang_n), kind, lang_n), url)
    return _with_url(_wake(text, kind, lang_n), url)


def _strip_think(text: str) -> str:
    """Grok reasoning is wrapped in <think> when Remove CoT is off on invoke_llm."""
    text = _THINK_BLOCK.sub("", text or "")
    text = _THINK_UNTIL_CLOSE.sub("", text)
    return text.strip()


def _wake(text: str, kind: str, lang: str) -> str:
    """Group invitees don't know the wake rules; say them verbatim, never rewritten."""
    import i18n

    if kind != "product_invite":
        return text
    return f"{text}\n\n{i18n.wake_hint(lang)}"


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
