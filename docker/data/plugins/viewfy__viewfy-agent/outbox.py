"""Poll Viewfy diegetic outbox → LangBot personality rewrite → send via LangBot."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from main import ViewfyAgentPlugin

log = logging.getLogger("viewfy_agent.outbox")

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_UNTIL_CLOSE = re.compile(r"^.*?</think>", re.DOTALL | re.IGNORECASE)

POLL_SEC = 20
DELIVER_RETRIES = 2  # attempts after the first (= 3 total)
DELIVER_RETRY_SEC = 3.0

# Telegram reports "group chat was deleted" even when the chat still exists
# and getChatMember is `left` / `kicked`. Do not DM-fallback those.
_GROUP_GONE = (
    "group chat was deleted",
    "bot was kicked",
    "bot is not a member",
    "chat not found",
)


class GroupGone(RuntimeError):
    """Bot cannot send to this group; pin should be dropped."""


async def _send_formatted(
    plugin: ViewfyAgentPlugin,
    chat_id: str,
    body_text: str,
    markup: dict[str, Any] | None,
) -> bool:
    """MarkdownV2 (markdown_card) → HTML → plain. Always the same chat."""
    import cta

    attempts: list[tuple[str | None, str]] = [
        ("MarkdownV2", cta.markdownv2_from_markdownish(body_text)[:4000]),
        ("HTML", cta.html_from_markdownish(body_text)[:4000]),
        (None, body_text[:4000]),
    ]
    last_desc: str | None = None
    for parse_mode, payload in attempts:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": payload,
            "disable_web_page_preview": True,
            "link_preview_options": {"is_disabled": True},
        }
        if parse_mode:
            body["parse_mode"] = parse_mode
        if markup:
            body["reply_markup"] = markup
        out = await plugin._tg("sendMessage", body)
        if out.get("ok"):
            return True
        last_desc = str(out.get("description") or "")
        log.warning("outbox %s send failed: %s", parse_mode or "plain", last_desc)
        if _group_gone(chat_id, last_desc):
            raise GroupGone(last_desc or "bot left group")
    return False


def _group_gone(chat_id: str, description: str | None) -> bool:
    if not str(chat_id).startswith("-"):
        return False
    hay = (description or "").lower()
    return any(s in hay for s in _GROUP_GONE)

REWRITE_SYSTEM = """You are Viewfy, chatting with a young YC founder over Telegram.

Voice: peer founder, not coach or corporate SaaS. Short, direct, slightly sharp.
Casual but not cringe. No hype adjectives. No LinkedIn energy.

You are rewriting an INTERNAL event into a chat update for the founder.
Rules:
- First line is what happened. No preamble. No "Great news!".
- Diegetic only: never show UUIDs, internal ids, status enums, action_id, or JSON keys.
- Talk in product names, domains, and channel names (HN, Reddit, etc.).
- Do NOT include the draft body. Do NOT invent draft text. Framing only.
- Do NOT invent numbers. Only report counts present in facts_json. Omit zero/missing sections.
- If needs_approval is true (and kind is not daily_digest), say a draft is ready. Do not say
  "reply approve or reject" (Queue it / Skip buttons are attached). Never imply the bot
  posts it: queueing hands the reply to the founder (extension) or the operator to post.
- If rung_phrase is present, say what the comment does in plain language
  (just advice / names the product / includes a link). If menu_phrase is present, say what
  that community allows (advice-only / name but no link / name and a link). Never say
  "rung", "1-3", "1-4", or "room 1-3".
- If kind is product_invite_accepted / outcome accepted: say they are in on the product, briefly.
  Name the person ONLY as member_name spells it, verbatim. If member_name is missing, say it
  without any name. Never guess or invent a name.
- If kind is pr_ready / outcome open: say the SEO fix PR is live on GitHub. Do not paste the URL
  (a button is attached). Mention repo or PR number lightly if useful.
- If kind is blog_published / outcome published: the post the founder asked for is live on
  their blog. Name the title, one line. Do not paste the URL (a button is attached).
- If kind is blog_published / outcome draft: a blog post is written and waiting. Name the
  title, one line. Do not say it is live. Do not paste the URL (View / Approve / Reject
  buttons are attached). Do not say "reply approve or reject".
- If kind is daily_digest / outcome morning: this is the morning report. Open like
  "morning. here is {day}, while you were shipping." then short lines for each non-empty
  section (visitors, signups, citations, post, ads total, link building, sales, scout).
  Keep link and sales clearly separate. If needs_you is present, end with what needs them;
  otherwise close with go build. Quiet day if almost nothing landed.
- For non-digest kinds: keep replies tight, 1-2 short lines.
- On failure: say what blocked and the next move, plainly.
- Celebrate real wins lightly with 🎉. Occasional blue emoji ok. No spam.
- Never use em dashes or en dashes. Use comma, period, colon, or ASCII hyphen (-).
- Output ONLY the chat text. No quotes, no markdown fences, no labels, no links
  (the sender appends the draft and buttons separately).
"""


def _facts_user_message(kind: str, payload: dict[str, Any], lang: str) -> str:
    import i18n

    # Strip fields the rewriter must not paraphrase into chat.
    safe = {
        k: v
        for k, v in payload.items()
        if k not in ("draft_text", "action_id")
    }
    if kind == "daily_digest":
        shape = (
            "Rewrite these facts into a complete morning Telegram report "
            "(greeting + one short line per non-empty section). "
            "Do not invent metrics."
        )
        max_hint = "Aim for under ~12 short lines."
    else:
        shape = "Rewrite these facts into a short diegetic Telegram intro (1-2 lines)."
        max_hint = ""
    lang_line = i18n.prompt_lang_line(lang)
    return (
        f"{shape}\n"
        f"{lang_line}\n"
        f"{max_hint}\n"
        f"kind: {kind}\n"
        f"facts_json: {json.dumps(safe, ensure_ascii=False)}"
    ).strip()


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


async def rewrite(
    plugin: ViewfyAgentPlugin,
    kind: str,
    payload: dict[str, Any],
    *,
    lang: str = "en",
) -> str:
    from langbot_plugin.api.entities.builtin.provider import message as provider_message
    import i18n

    model = (plugin.llm_model_uuid or "").strip()
    if not model:
        raise RuntimeError("llm_model_uuid not configured")

    lang_n = i18n.normalize_lang(lang)
    system = REWRITE_SYSTEM
    if lang_n != "en":
        system = system.rstrip() + "\n\n" + i18n.prompt_lang_line(lang_n)

    max_tokens = 420 if kind == "daily_digest" else 180
    msg = await plugin.invoke_llm(
        llm_model_uuid=model,
        messages=[
            provider_message.Message(role="system", content=system),
            provider_message.Message(
                role="user", content=_facts_user_message(kind, payload, lang_n)
            ),
        ],
        extra_args={"max_tokens": max_tokens},
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
    text = _strip_think(text)
    if not text:
        raise RuntimeError("empty LLM rewrite")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    # Never let a confused rewrite leak ids
    for bad in ("action_id",):
        if bad in text:
            text = text.replace(bad, "")
    text = _scrub_dashes(text)
    limit = 2000 if kind == "daily_digest" else 800
    return text[:limit]


def _strip_think(text: str) -> str:
    """Grok reasoning is wrapped in <think> when Remove CoT is off on invoke_llm."""
    text = _THINK_BLOCK.sub("", text or "")
    text = _THINK_UNTIL_CLOSE.sub("", text)
    return text.strip()


def _scrub_dashes(text: str) -> str:
    """Models ignore the no-em-dash rule; force ASCII hyphen before send."""
    return (
        (text or "")
        .replace("\u2014", "-")  # em dash
        .replace("\u2013", "-")  # en dash
        .replace("\u2212", "-")  # minus
    )


def _blog_approval_row(payload: dict[str, Any], lang: str) -> dict[str, Any] | None:
    """Exactly one row: View, Approve, Reject. Nothing else."""
    import cta
    import i18n

    action_id = (payload.get("action_id") or "").strip()
    view = (payload.get("button_url") or payload.get("target_url") or "").strip()
    if not action_id or not view.startswith("https://"):
        return None
    return cta.keyboard([[
        cta.url_btn(i18n.t(lang, "view_btn"), view),
        cta.cb_btn(i18n.t(lang, "blog_approve_btn"), f"vf:approve:{action_id}", style="primary"),
        cta.cb_btn(i18n.t(lang, "blog_reject_btn"), f"vf:reject:{action_id}", style="danger"),
    ]])


def _build_markup(
    payload: dict[str, Any],
    lang: str,
    *,
    kind: str = "",
) -> dict[str, Any] | None:
    import cta
    import i18n

    if payload.get("needs_approval") and (
        kind == "blog_published" or payload.get("approve_with") == "blog"
    ):
        return _blog_approval_row(payload, lang)

    rows: list[list[dict[str, Any]]] = []

    button_url = (payload.get("button_url") or "").strip()
    if button_url.startswith("https://"):
        key = payload.get("button_key") or "pr_btn"
        rows.append([cta.url_btn(i18n.t(lang, key), button_url)])

    # Morning digest: one Review drafts CTA → unified approvals queue (all motions).
    if kind == "daily_digest":
        product_id = (payload.get("product_id") or "").strip()
        if payload.get("needs_approval") and product_id:
            rows.append([
                cta.cb_btn(
                    i18n.t(lang, "review_drafts_btn"),
                    f"vf:queue:{product_id}",
                    style="primary",
                )
            ])
        return cta.keyboard(rows) if rows else None

    target_url = (payload.get("target_url") or "").strip()
    action_id = (payload.get("action_id") or "").strip()
    if payload.get("needs_approval"):
        # Scout: Copy draft + Open thread + Skip.
        # A draft card only exists when the channel's autopost is off, so no
        # button may read as the bot posting. Copy is omitted only when the
        # draft exceeds Telegram's 256-char copy_text cap (the fenced block
        # in the message body stays tap-to-copy there).
        top = []
        draft = (payload.get("draft_text") or "").strip()
        if draft and len(draft) <= 256:
            top.append(cta.copy_btn(i18n.t(lang, "copy_btn"), draft))
        if target_url.startswith("https://") and target_url != button_url:
            top.append(cta.url_btn(i18n.t(lang, "thread_btn"), target_url))
        if top:
            rows.append(top)
        if action_id:
            rows.append([
                cta.cb_btn(i18n.t(lang, "reject_btn"), f"vf:reject:{action_id}", style="danger"),
            ])

    return cta.keyboard(rows) if rows else None



async def deliver(plugin: ViewfyAgentPlugin, item: dict[str, Any]) -> str:
    import cta

    payload = item.get("payload") or {}
    kind = item.get("kind") or "report"
    tg_uid = str(item["telegram_user_id"])
    chat_id = str(item.get("telegram_chat_id") or tg_uid)
    lang = plugin.lang_for(tg_uid)
    intro = await rewrite(plugin, kind, payload, lang=lang)

    if (
        kind != "daily_digest"
        and payload.get("needs_approval")
        and (payload.get("draft_text") or "").strip()
    ):
        text = _compose_approval(intro, payload)
    else:
        text = intro

    # New members don't know the group wake rules; state them verbatim once.
    if kind == "product_invite_accepted" and chat_id.startswith("-"):
        import i18n

        text = f"{text}\n\n{i18n.wake_hint(lang)}"

    # Digest: Review drafts CTA when needs_approval. Per-draft Approve/Reject stay on scout.
    markup = _build_markup(payload, lang, kind=kind)

    # Bot API first so we can attach buttons. MarkdownV2 matches LangBot markdown_card;
    # HTML then plain+markup stay on the same chat if Telegram rejects the parse mode.
    sent = False
    chat = chat_id.split("#", 1)[0]
    if getattr(plugin, "bot_token", None):
        body_text = text
        if markup and any(
            "url" in b for row in markup.get("inline_keyboard", []) for b in row
        ):
            body_text = cta.strip_urls(text) or text
        sent = await _send_formatted(plugin, chat, body_text, markup)
        if not sent:
            log.warning("outbox formatted send failed chat=%s", chat)

    if not sent:
        from langbot_plugin.api.entities.builtin.platform import message as platform_message

        bot = (plugin.bot_uuid or "").strip()
        if not bot:
            raise RuntimeError("bot_uuid not configured")
        chain = platform_message.MessageChain([platform_message.Plain(text=text)])
        target_type = "group" if chat.startswith("-") else "person"
        await plugin.send_message(
            bot_uuid=bot,
            target_type=target_type,
            target_id=chat if target_type == "group" else tg_uid,
            message_chain=chain,
        )

    await plugin.ingest(
        telegram_user_id=tg_uid,
        telegram_chat_id=chat,
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


async def _deliver_with_retries(plugin: ViewfyAgentPlugin, item: dict[str, Any]) -> str:
    attempts = 1 + DELIVER_RETRIES
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await deliver(plugin, item)
        except GroupGone:
            raise
        except Exception as e:
            last = e
            if i + 1 >= attempts:
                break
            log.warning(
                "outbox deliver retry id=%s attempt=%s/%s err=%s",
                item.get("id"),
                i + 1,
                attempts,
                e,
            )
            await asyncio.sleep(DELIVER_RETRY_SEC * (i + 1))
    assert last is not None
    raise last


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
            text = await _deliver_with_retries(plugin, item)
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
