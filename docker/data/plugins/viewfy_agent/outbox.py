"""Poll Viewfy diegetic outbox → LangBot personality rewrite → send via LangBot."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from main import ViewfyAgentPlugin

log = logging.getLogger("viewfy_agent.outbox")

POLL_SEC = 20

REWRITE_SYSTEM = """You are Viewfy, chatting with a young YC founder over Telegram.

Voice: peer founder, not coach or corporate SaaS. Short, direct, slightly sharp.
Casual but not cringe. No hype adjectives. No LinkedIn energy.

You are rewriting an INTERNAL event into a short chat update for the founder.
Rules:
- Keep replies tight: 1-2 short lines for the intro only.
- First line is what happened. No preamble. No "Great news!".
- Diegetic only: never show UUIDs, internal ids, status enums, action_id, or JSON keys.
- Talk in product names, domains, and channel names (HN, Reddit, etc.).
- Do NOT include the draft body. Do NOT invent draft text. Framing only.
- If needs_approval is true, say a draft is ready. Do not say "reply approve or reject"
  (Approve/Reject buttons are attached).
- If kind is product_invite_accepted / outcome accepted: say they are in on the product, briefly.
- If kind is pr_ready / outcome open: say the SEO fix PR is live on GitHub. Do not paste the URL
  (a button is attached). Mention repo or PR number lightly if useful.
- On failure: say what blocked and the next move, plainly.
- Celebrate real wins lightly with 🎉. Occasional blue emoji ok. No spam.
- Never use em dashes or en dashes. Use comma, period, colon, or ASCII hyphen (-).
- Output ONLY the intro text. No quotes, no markdown fences, no labels, no links
  (the sender appends the draft and buttons separately).
"""


def _facts_user_message(kind: str, payload: dict[str, Any]) -> str:
    # Strip fields the rewriter must not paraphrase into chat.
    safe = {
        k: v
        for k, v in payload.items()
        if k not in ("draft_text", "action_id")
    }
    return (
        "Rewrite these facts into a short diegetic Telegram intro (1-2 lines).\n"
        f"kind: {kind}\n"
        f"facts_json: {json.dumps(safe, ensure_ascii=False)}"
    )


def _compose_approval(intro: str, payload: dict[str, Any]) -> str:
    """Literal draft in the chat. Trust boundary: never summarize draft_text."""
    draft = (payload.get("draft_text") or "").strip()
    parts = [intro.strip()]
    if draft:
        parts.append("")
        parts.append("```")
        parts.append(draft)
        parts.append("```")
    return "\n".join(parts).strip()


def _html_inline(s: str) -> str:
    """Escape then apply markdown-ish link/bold on a non-code segment."""
    parts: list[str] = []
    pos = 0
    for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)]+)\)|\*\*(.+?)\*\*", s):
        parts.append(html.escape(s[pos:m.start()]))
        if m.group(1) is not None:
            parts.append(
                f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'
            )
        else:
            parts.append(f"<b>{html.escape(m.group(3))}</b>")
        pos = m.end()
    parts.append(html.escape(s[pos:]))
    return "".join(parts)


def _html_from_markdownish(text: str) -> str:
    """Best-effort HTML for Telegram parse_mode=HTML (links + pre + bold)."""
    out: list[str] = []
    pos = 0
    for m in re.finditer(r"```(?:\w+)?\n(.*?)```", text, flags=re.DOTALL):
        out.append(_html_inline(text[pos:m.start()]))
        out.append(f"<pre>{html.escape(m.group(1))}</pre>")
        pos = m.end()
    out.append(_html_inline(text[pos:]))
    return "".join(out)


async def rewrite(plugin: ViewfyAgentPlugin, kind: str, payload: dict[str, Any]) -> str:
    from langbot_plugin.api.entities.builtin.provider import message as provider_message

    model = (plugin.llm_model_uuid or "").strip()
    if not model:
        raise RuntimeError("llm_model_uuid not configured")

    msg = await plugin.invoke_llm(
        llm_model_uuid=model,
        messages=[
            provider_message.Message(role="system", content=REWRITE_SYSTEM),
            provider_message.Message(role="user", content=_facts_user_message(kind, payload)),
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
    # Never let a confused rewrite leak ids
    for bad in ("action_id",):
        if bad in text:
            text = text.replace(bad, "")
    return text[:800]


def _build_markup(payload: dict[str, Any], lang: str) -> dict[str, Any] | None:
    import cta
    import i18n

    rows: list[list[dict[str, Any]]] = []

    button_url = (payload.get("button_url") or "").strip()
    if button_url.startswith("https://"):
        key = payload.get("button_key") or "pr_btn"
        rows.append([cta.url_btn(i18n.t(lang, key), button_url)])

    target_url = (payload.get("target_url") or "").strip()
    if (
        payload.get("needs_approval")
        and target_url.startswith("https://")
        and target_url != button_url
    ):
        rows.append([cta.url_btn(i18n.t(lang, "thread_btn"), target_url)])

    action_id = (payload.get("action_id") or "").strip()
    if payload.get("needs_approval") and action_id:
        # Telegram callback_data max 64 bytes; uuid + prefix fits.
        rows.append([
            cta.cb_btn(i18n.t(lang, "approve_btn"), f"vf:approve:{action_id}", style="success"),
            cta.cb_btn(i18n.t(lang, "reject_btn"), f"vf:reject:{action_id}", style="danger"),
        ])

    return cta.keyboard(rows) if rows else None


async def deliver(plugin: ViewfyAgentPlugin, item: dict[str, Any]) -> str:
    import cta

    payload = item.get("payload") or {}
    kind = item.get("kind") or "report"
    intro = await rewrite(plugin, kind, payload)

    if payload.get("needs_approval") and (payload.get("draft_text") or "").strip():
        text = _compose_approval(intro, payload)
    else:
        text = intro

    tg_uid = str(item["telegram_user_id"])
    chat_id = str(item.get("telegram_chat_id") or tg_uid)
    lang = plugin.lang_for(tg_uid)
    markup = _build_markup(payload, lang)

    # Prefer Bot API HTML so links + preformatted draft render even without markdown_card.
    sent = False
    if getattr(plugin, "bot_token", None):
        body_text = text
        if markup and any(
            "url" in b for row in markup.get("inline_keyboard", []) for b in row
        ):
            body_text = cta.strip_urls(text) or text
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": _html_from_markdownish(body_text)[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if markup:
            body["reply_markup"] = markup
        out = await plugin._tg("sendMessage", body)
        sent = bool(out.get("ok"))
        if not sent:
            log.warning("outbox HTML send failed: %s", out.get("description"))

    if not sent:
        from langbot_plugin.api.entities.builtin.platform import message as platform_message

        bot = (plugin.bot_uuid or "").strip()
        if not bot:
            raise RuntimeError("bot_uuid not configured")
        chain = platform_message.MessageChain([platform_message.Plain(text=text)])
        await plugin.send_message(
            bot_uuid=bot,
            target_type="person",
            target_id=tg_uid,
            message_chain=chain,
        )

    await plugin.ingest(
        telegram_user_id=tg_uid,
        telegram_chat_id=chat_id,
        direction="out",
        text=text,
        meta={
            "kind": kind,
            "outbox_id": str(item.get("id")),
            "via": "langbot_outbox",
            "action_id": payload.get("action_id"),
            "needs_approval": bool(payload.get("needs_approval")),
            "has_buttons": bool(markup),
        },
    )
    return text


async def poll_once(plugin: ViewfyAgentPlugin) -> int:
    out = await plugin._request("POST", "/api/telegram/agent/outbox/claim", query={"limit": "10"})
    if out.get("_http_status"):
        log.warning("outbox claim failed: %s", out.get("detail") or out)
        return 0
    items = out.get("items") or []
    done = 0
    for item in items:
        item_id = item.get("id")
        try:
            text = await deliver(plugin, item)
            await plugin._request(
                "POST",
                f"/api/telegram/agent/outbox/{item_id}/ack",
                body={"status": "sent", "text": text},
            )
            done += 1
        except Exception as e:
            log.exception("outbox deliver failed id=%s", item_id)
            try:
                await plugin._request(
                    "POST",
                    f"/api/telegram/agent/outbox/{item_id}/ack",
                    body={"status": "failed", "error": str(e)[:500]},
                )
            except Exception:
                log.exception("outbox ack failed id=%s", item_id)
    return done


async def loop(plugin: ViewfyAgentPlugin) -> None:
    log.info("outbox poller started interval=%ss", POLL_SEC)
    while True:
        try:
            n = await poll_once(plugin)
            if n:
                log.info("outbox delivered %s", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("outbox poll crashed")
        await asyncio.sleep(POLL_SEC)
