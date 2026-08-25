from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events
from langbot_plugin.api.entities.builtin.platform import message as platform_message
from langbot_plugin.api.entities.builtin.provider import message as provider_message

_START_RE = re.compile(r"^/start(?:@[A-Za-z0-9_]+)?(?:\s|$)", re.IGNORECASE)

# Sibling i18n lives at plugin root.
_root = str(Path(__file__).resolve().parents[2])
if _root not in sys.path:
    sys.path.insert(0, _root)
import i18n  # noqa: E402

log = logging.getLogger("viewfy_agent.ingest")

CLOCK_PREFIX = "Clock:"
GROUP_CTX_PREFIX = "Recent group chat"
QUOTE_PREFIX = "Replying to"
GROUP_CTX_LINES = 12
GROUP_CTX_LINE_CHARS = 300


def _clock_line(plugin, tg_id: str) -> str:
    from datetime import timedelta

    now, zone = plugin.now_for(tg_id)
    return (
        f"{CLOCK_PREFIX} it is {now:%H:%M} on {now:%A %Y-%m-%d} in {zone}, the founder's "
        f"timezone. Today is {now:%Y-%m-%d}; yesterday was {now - timedelta(days=1):%Y-%m-%d}. "
        "Never work the date out from tool output - traffic stats end at the last complete "
        "day, so their newest bucket is yesterday. Say dates the way the founder would."
    )


FORMAT_LINE = (
    "Format replies in Markdown for Telegram: use **bold** and fenced code blocks for "
    "draft or post bodies. Do NOT paste CTA https links or markdown [label](url) for "
    "connect, invite, PR, or thread opens — Telegram attaches those as inline buttons "
    "from tool results / outbox. Never show raw UUIDs or internal action ids in chat "
    "(except when the user message already contains action_id= from an Approve/Reject button). "
    "Never invent a product URL. Call scrape only with a URL the founder typed in this chat; "
    "before creating a product, echo the exact URL and get a clear yes. Bare hey/@ pings are "
    "not consent to scrape. If unsure, ask. "
    "Never claim you started, finished, or will perform a side effect unless you called a "
    "writable tool that can do that side effect and it succeeded. If no listed tool can do "
    "it, decline in one short line. Read-only tools never count as doing the work — but "
    "reading, reviewing, and comparing what they return is work you can do. "
    "For users per day / site traffic / pageviews, call stats (Viewfy tracker). "
    "Never say you lack traffic metrics. Never ask for GA, Plausible, or Cloudflare Analytics. "
    "For a morning report / daily status / what happened while shipping, call report "
    "(optional day=YYYY-MM-DD, default yesterday). Same facts as the cron digest — do not "
    "stitch from stats + links + scout_queue. "
    "For inbox send: real subject, short body, low volume while warming (not blasts). "
    "Do not claim Gmail shows a brand avatar without CMC/VMC. "
    "Cloudflare is only for blog DNS/worker and inbox email — check blog/inbox/products "
    "status fields before claiming Cloudflare is not connected. "
    "For GitHub: call products (id) and read github.connected / github.repos before "
    "claiming no repo is linked. Connect with connect platform=github (Telegram button); "
    "never invent Settings -> Repository. "
    "If the founder message is 'approve action_id=...' or 'reject action_id=...', call "
    "scout_approve with that id and decision immediately (skip scout_queue). The same "
    "tool also publishes or skips a blog draft that used those buttons. "
    "If they type approve/reject without an id, call scout_queue then scout_approve. "
    "If they want to change a waiting draft (rewrite this comment, here's the text, make "
    "this one shorter), call scout_approve decision=edit with that action_id and the full "
    "new draft_text. Do not use scout action=revise for a single draft: revise is the "
    "standing brief for future drafts. If they type edit without an id, call scout_queue "
    "then scout_approve decision=edit. "
    "If the founder message is 'show approvals product_id=...', call approvals with that "
    "product_id immediately and list every pending draft (link, sales, scout)."
)

GROUP_LINE = (
    "This message is in a Telegram group. Reply briefly in the group. "
    "Tools still run as the mentioning user (their linked Viewfy account). "
    "If an unlinked speaker is in a pinned group, the runtime offers a product invite "
    "link (diegetic) — do not invent access or ask them to DM /start for that case. "
    "Unlinked in an unpinned group or DM: Connect Viewfy offer. "
    "To lock this group to one customer product, the owner should call group_pin set."
)


def _plain_text(message_chain) -> str:
    if message_chain is None:
        return ""
    parts: list[str] = []
    for component in message_chain:
        if isinstance(component, platform_message.Quote):
            continue  # the quoted original is context, not this message's text
        if isinstance(component, platform_message.Plain):
            parts.append(component.text or "")
        elif hasattr(component, "text") and getattr(component, "text", None):
            parts.append(str(component.text))
    return "".join(parts).strip()


def _quote_of(message_chain) -> dict | None:
    """The replied-to message from the chain's Quote component, if any."""
    if message_chain is None:
        return None
    for component in message_chain:
        if isinstance(component, platform_message.Quote):
            text = _plain_text(component.origin)
            if not text:
                return None
            return {
                "message_id": str(component.id) if component.id is not None else None,
                "sender_id": str(component.sender_id)
                if component.sender_id is not None
                else None,
                "text": text,
            }
    return None


def _message_text(content) -> str:
    """Normalize LLM message content (str | list | None) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for el in content:
            if isinstance(el, str):
                parts.append(el)
            elif isinstance(el, dict):
                parts.append(str(el.get("text") or el.get("content") or ""))
            else:
                parts.append(str(getattr(el, "text", None) or getattr(el, "content", None) or ""))
        return "".join(parts).strip()
    return str(content).strip()


def _message_id(message_chain) -> str | None:
    try:
        mid = message_chain.message_id
        if mid is None or mid == -1:
            return None
        return str(mid)
    except Exception:
        return None


def _content_blob(messages) -> str:
    blobs: list[str] = []
    for m in messages or []:
        c = getattr(m, "content", None)
        if isinstance(c, str):
            blobs.append(c)
        elif isinstance(c, dict) and c.get("content"):
            blobs.append(str(c["content"]))
    return "\n".join(blobs)


def _sender_id_from_prompt_event(event) -> str:
    """Prefer the human sender (works for groups); fall back to session parse."""
    query = getattr(event, "query", None)
    sid = getattr(query, "sender_id", None) if query is not None else None
    if sid is not None and str(sid).strip():
        return str(sid)
    session = str(getattr(event, "session_name", "") or "")
    if session.startswith("person_"):
        return session.split("_", 1)[-1]
    return ""


async def _group_context_block(plugin, chat_id: str, current_text: str) -> str:
    """Ambient group chatter as one labeled system block.

    LangBot's conversation only holds turns that triggered the bot; messages
    between other humans never reach it, so pronouns like "покажи його" lose
    their referent. Viewfy's message store has every inbound row — pull the
    recent ones for this chat and label speakers.
    """
    try:
        out = await plugin._request(
            "GET",
            "/api/telegram/agent/messages",
            query={"telegram_chat_id": chat_id, "limit": "20"},
        )
    except Exception:
        log.exception("group context fetch failed")
        return ""
    if out.get("_http_status"):
        return ""

    cur = (current_text or "").strip()
    lines: list[str] = []
    for row in out.get("items") or []:
        if (row.get("direction") or "") != "in":
            continue
        text = (row.get("text") or "").strip()
        if not text or text == cur:
            continue
        who = (row.get("sender_name") or "").strip() or f"tg:{row.get('telegram_user_id')}"
        lines.append(f"[{who}]: {text[:GROUP_CTX_LINE_CHARS]}")
    lines = lines[-GROUP_CTX_LINES:]
    if not lines:
        return ""
    return (
        f"{GROUP_CTX_PREFIX} (ambient context, oldest first; most of these were "
        "not addressed to you and need no reply, but the current message may "
        "refer to them. Exception: if the CURRENT speaker sent you a clear, "
        "still-unanswered request moments before this message, handle that "
        "request too instead of silently skipping it):\n" + "\n".join(lines)
    )


async def _inject_recent_outs(plugin, tg_id: str, event) -> None:
    """Pull proactive outs (outbox drafts) into LangBot conversation history."""
    try:
        out = await plugin._request(
            "GET",
            "/api/telegram/agent/messages",
            query={"telegram_user_id": tg_id, "limit": "20"},
        )
    except Exception:
        log.exception("list messages failed")
        return
    if out.get("_http_status"):
        return

    items = out.get("items") or []
    existing = _content_blob(getattr(event, "prompt", None))
    prompt = list(getattr(event, "prompt", None) or [])
    added = 0
    for row in items:
        if (row.get("direction") or "") != "out":
            continue
        text = (row.get("text") or "").strip()
        if not text or len(text) < 20:
            continue
        meta = row.get("meta") or {}
        if meta.get("via") != "langbot_outbox" and not meta.get("needs_approval"):
            if "approve" not in text.lower() and "```" not in text:
                continue
        needle = text[:120]
        if needle in existing:
            continue
        prompt.append(provider_message.Message(role="assistant", content=text))
        existing += "\n" + text
        added += 1
    if added:
        event.prompt = prompt
        log.info("injected %s outbox message(s) into prompt for %s", added, tg_id)


async def _handle_inbound(plugin, event_context, *, event_name: str, is_group: bool) -> None:
    event = event_context.event
    text = _plain_text(event.message_chain)
    # Mention-only group pings become empty after @ is stripped — still greet.
    if not text and is_group:
        text = "hey"
    if not text:
        return

    tg_id = str(event.sender_id)
    chat_id = str(event.launcher_id).split("#", 1)[0]
    plugin.turn_start(chat_id)
    quote = _quote_of(event.message_chain)
    plugin.set_quote(chat_id, quote)
    lang = plugin.remember_lang(
        tg_id,
        i18n.resolve_lang(
            event=event,
            text=text,
            previous=plugin.lang_for(tg_id),
        ),
    )

    meta = {"event": event_name, "lang": lang, "group": is_group}
    if quote:
        meta["reply_to_message_id"] = quote.get("message_id")
        meta["reply_to_snippet"] = quote["text"][:300]
    await plugin.ingest(
        telegram_user_id=tg_id,
        telegram_chat_id=chat_id,
        direction="in",
        text=text,
        telegram_message_id=_message_id(event.message_chain),
        meta=meta,
    )

    # Unlinked + pinned group: mint product invite (diegetic) before the LLM runs.
    if is_group and not _START_RE.match(text):
        try:
            me = await plugin.me(tg_id)
        except Exception:
            me = {}
        if not me.get("linked"):
            try:
                pin = await plugin._request(
                    "GET",
                    "/api/telegram/agent/group_pin",
                    query={"telegram_chat_id": chat_id},
                )
            except Exception:
                pin = {}
            if pin.get("pinned"):
                event_context.prevent_default()
                event_context.prevent_postorder()
                url = None
                try:
                    reply, url = await plugin.offer_product_invite(
                        telegram_user_id=tg_id,
                        telegram_chat_id=chat_id,
                        lang=lang,
                    )
                except Exception as e:
                    log.exception("product invite offer failed")
                    reply = i18n.prepare_fail(lang, str(e))
                sent = await plugin.send_connect_message(
                    chat_id, reply, url, lang=lang, button_key="invite_btn", sticker=False
                )
                if not sent:
                    fallback = reply if not url else f"{reply}\n\n{url}"
                    await event_context.reply(
                        platform_message.MessageChain([platform_message.Plain(text=fallback)])
                    )
                    reply = fallback
                await plugin.ingest(
                    telegram_user_id=tg_id,
                    telegram_chat_id=chat_id,
                    direction="out",
                    text=reply,
                    meta={
                        "kind": "product_invite",
                        "via": "diegetic",
                        "url": url,
                        "lang": lang,
                        "group": True,
                    },
                )
                return

    if not _START_RE.match(text):
        return

    # /start: diegetic Connect offer; bind is still per user.
    event_context.prevent_default()
    event_context.prevent_postorder()

    url = None
    try:
        reply, url = await plugin.start_reply(tg_id, lang)
    except Exception as e:
        reply = i18n.prepare_fail(lang, str(e))

    if is_group:
        # In a group nothing tells a new user the bot only answers when
        # addressed; say it once, verbatim.
        reply = f"{reply}\n\n{i18n.wake_hint(lang)}"

    sent = await plugin.send_connect_message(chat_id, reply, url, lang=lang)
    if not sent:
        fallback = reply if not url else f"{reply}\n\n{url}"
        await event_context.reply(
            platform_message.MessageChain([platform_message.Plain(text=fallback)])
        )
        reply = fallback

    await plugin.ingest(
        telegram_user_id=tg_id,
        telegram_chat_id=chat_id,
        direction="out",
        text=reply,
        meta={
            "command": "start",
            "kind": "connect_offer",
            "via": "diegetic",
            "url": url,
            "lang": lang,
            "group": is_group,
        },
    )


class IngestListener(EventListener):
    async def initialize(self) -> None:
        await super().initialize()

        @self.handler(events.PersonMessageReceived)
        async def on_person(event_context: context.EventContext) -> None:
            await _handle_inbound(
                self.plugin, event_context, event_name="PersonMessageReceived", is_group=False
            )

        @self.handler(events.GroupMessageReceived)
        async def on_group(event_context: context.EventContext) -> None:
            event = event_context.event
            text = _plain_text(event.message_chain)
            # After AtBotRule strips @bot, mention-only pings become empty.
            # Leave a short Plain so the LLM still has a user turn.
            if text and not re.sub(r"@[\w]+", "", text).strip():
                try:
                    event.message_chain.append(platform_message.Plain(text=" hey"))
                except Exception:
                    pass
            await _handle_inbound(
                self.plugin, event_context, event_name="GroupMessageReceived", is_group=True
            )

        @self.handler(events.PromptPreProcessing)
        async def on_prompt(event_context: context.EventContext) -> None:
            """Language, markdown formatting, group pin hint, reinject outbox."""
            event = event_context.event
            tg_id = _sender_id_from_prompt_event(event)
            session = str(getattr(event, "session_name", "") or "")
            is_group = session.startswith("group_")
            query = getattr(event, "query", None)
            chat_id = ""
            if query is not None and getattr(query, "launcher_id", None) is not None:
                chat_id = str(query.launcher_id).split("#", 1)[0]
            elif session.startswith("group_"):
                chat_id = session.split("_", 1)[-1]
            elif session.startswith("person_"):
                chat_id = session.split("_", 1)[-1]

            # LLM is about to run (and again after tool rounds) — keep typing alive.
            if chat_id:
                self.plugin.start_typing(chat_id)

            import side_effect

            prompts = list(event.default_prompt or [])
            # Replaced, not appended: a turn rebuilds this prompt once per tool
            # round and stale clocks / group snapshots would pile up inside one
            # conversation.
            prompts = [
                m for m in prompts
                if not str(getattr(m, "content", "") or "").startswith(CLOCK_PREFIX)
                and not str(getattr(m, "content", "") or "").startswith(GROUP_CTX_PREFIX)
                and not str(getattr(m, "content", "") or "").startswith(QUOTE_PREFIX)
            ]
            prompts.append(
                provider_message.Message(role="system", content=_clock_line(self.plugin, tg_id))
            )
            quote = self.plugin.get_quote(chat_id)
            if quote:
                prompts.append(
                    provider_message.Message(
                        role="system",
                        content=(
                            f"{QUOTE_PREFIX}: the current message is a Telegram reply "
                            f"to this earlier message: \"{quote['text'][:600]}\". "
                            "Words like 'this', 'here', 'it' likely point at it."
                        ),
                    )
                )
            if FORMAT_LINE not in _content_blob(prompts):
                prompts.append(provider_message.Message(role="system", content=FORMAT_LINE))
            caps = side_effect.capability_line()
            if caps not in _content_blob(prompts):
                prompts.append(provider_message.Message(role="system", content=caps))
            if is_group and GROUP_LINE not in _content_blob(prompts):
                prompts.append(provider_message.Message(role="system", content=GROUP_LINE))

            if is_group and chat_id:
                try:
                    pin = await self.plugin._request(
                        "GET",
                        "/api/telegram/agent/group_pin",
                        query={"telegram_chat_id": chat_id},
                    )
                except Exception:
                    log.exception("group_pin fetch failed")
                    pin = {}
                if pin.get("pinned") and pin.get("product_name"):
                    domain = pin.get("product_domain") or ""
                    pin_line = (
                        f"This group is pinned to {pin['product_name']}"
                        + (f" ({domain})" if domain else "")
                        + ". Only work on that product. Do not list or act on other products. "
                        "Server enforcement applies; never show product ids in chat."
                    )
                    if pin_line not in _content_blob(prompts):
                        prompts.append(
                            provider_message.Message(role="system", content=pin_line)
                        )

            # DMs have no pin to anchor on, so name the founder's products instead.
            if not is_group and tg_id:
                anchor = await self.plugin.product_anchor(tg_id)
                if anchor and anchor not in _content_blob(prompts):
                    prompts.append(provider_message.Message(role="system", content=anchor))

            # Re-resolve every turn: client lc (via remembered), else script of this message.
            user_text = _message_text(
                next(
                    (
                        getattr(m, "content", None)
                        for m in reversed(list(getattr(event, "prompt", None) or []))
                        if getattr(m, "role", None) == "user"
                    ),
                    None,
                )
            )

            # Groups: absorb chatter that never triggered the bot (labeled by sender).
            if is_group and chat_id:
                block = await _group_context_block(self.plugin, chat_id, user_text)
                if block:
                    prompts.append(provider_message.Message(role="system", content=block))

            lang = "en"
            if tg_id:
                lang = self.plugin.remember_lang(
                    tg_id,
                    i18n.resolve_lang(
                        text=user_text,
                        previous=self.plugin.lang_for(tg_id),
                    ),
                )
            if lang != "en":
                line = i18n.prompt_lang_line(lang)
                # Bake into the main system prompt (stronger than a trailing system msg)
                # and repeat as the last system line (recency).
                merged = False
                for m in prompts:
                    if getattr(m, "role", None) != "system":
                        continue
                    content = getattr(m, "content", None)
                    if isinstance(content, str) and "LANGUAGE LOCK:" not in content:
                        m.content = content.rstrip() + "\n\n" + line
                        merged = True
                        break
                if not merged and line not in _content_blob(prompts):
                    prompts.append(provider_message.Message(role="system", content=line))
                # One trailing reminder (recency); drop any prior LANGUAGE LOCK reminders.
                reminder = (
                    f"Reminder: LANGUAGE LOCK is {lang}. "
                    f"Write the next reply fully in {i18n.LANG_NAMES.get(lang, lang)}."
                )
                prompts = [
                    m
                    for m in prompts
                    if not (
                        getattr(m, "role", None) == "system"
                        and isinstance(getattr(m, "content", None), str)
                        and str(m.content).startswith("Reminder: LANGUAGE LOCK")
                    )
                ]
                prompts.append(provider_message.Message(role="system", content=reminder))

            event.default_prompt = prompts

            if tg_id:
                await _inject_recent_outs(self.plugin, tg_id, event)

        @self.handler(events.NormalMessageResponded)
        async def on_responded(event_context: context.EventContext) -> None:
            import cta

            event = event_context.event
            text = (event.response_text or "").strip()
            if not text:
                return
            tg_id = str(event.sender_id)
            chat_id = str(event.launcher_id).split("#", 1)[0]

            funcs = list(event.funcs_called or [])
            turn = self.plugin.turn_state(chat_id)
            # Only round-final chunks carry tool_calls, and the terminal answer
            # carries none — so the turn's tools accrue here, one event at a time.
            turn["tools"].update(funcs)

            # Tool-call tracker lines ("Call foo...") — leave alone; keep typing.
            if text.startswith("Call ") and "..." in text:
                self.plugin.start_typing(chat_id)
                return

            # Final user-visible reply — clear the typing indicator.
            self.plugin.stop_typing(chat_id)

            # Mid-stream chunks and tool rounds: ingest only. `event.query` is
            # excluded from the event model out-of-process, so finish_reason is
            # the only finality signal we get (LangBot wrapper sets it).
            if str(getattr(event, "finish_reason", "stop") or "stop") != "stop":
                await self.plugin.ingest(
                    telegram_user_id=tg_id,
                    telegram_chat_id=chat_id,
                    direction="out",
                    text=text,
                    meta={
                        "event": "NormalMessageResponded",
                        "stream": "partial",
                        "funcs_called": list(event.funcs_called or []),
                        "lang": self.plugin.lang_for(tg_id),
                        "group": str(getattr(event, "launcher_type", "") or "").startswith("group")
                        or str(event.launcher_id).startswith("-"),
                    },
                )
                return

            # Lift webpage CTAs out of the body into Telegram inline buttons.
            # Skip fenced draft/code bodies for bare URLs; keep markdown links.
            # Also bail if a fence is still open (stream edge).
            if not cta.fences_balanced(text):
                await self.plugin.ingest(
                    telegram_user_id=tg_id,
                    telegram_chat_id=chat_id,
                    direction="out",
                    text=text,
                    meta={
                        "event": "NormalMessageResponded",
                        "stream": "open_fence",
                        "funcs_called": list(event.funcs_called or []),
                        "lang": self.plugin.lang_for(tg_id),
                        "group": str(getattr(event, "launcher_type", "") or "").startswith("group")
                        or str(event.launcher_id).startswith("-"),
                    },
                )
                return

            import side_effect

            lang = self.plugin.lang_for(tg_id)
            if not turn["declined"] and side_effect.should_decline(text, turn["tools"]):
                turn["declined"] = True
                original = text
                decline = side_effect.decline_text(lang)
                sent = False
                if getattr(self.plugin, "bot_token", None):
                    out = await self.plugin._tg(
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": decline[:4000],
                            "disable_web_page_preview": True,
                            "link_preview_options": {"is_disabled": True},
                        },
                    )
                    sent = bool(out.get("ok"))
                if sent:
                    event_context.prevent_default()
                else:
                    # No bot token / send failed — still rewrite what LangBot would emit.
                    event.response_text = decline
                await self.plugin.ingest(
                    telegram_user_id=tg_id,
                    telegram_chat_id=chat_id,
                    direction="out",
                    text=decline,
                    meta={
                        "event": "NormalMessageResponded",
                        "kind": "side_effect_declined",
                        "blocked_text": original,
                        "funcs_called": sorted(turn["tools"]),
                        "lang": lang,
                        "group": str(getattr(event, "launcher_type", "") or "").startswith("group")
                        or str(event.launcher_id).startswith("-"),
                    },
                )
                return

            outside = cta.outside_fences(text)
            links = cta.extract_links(outside)
            if links and getattr(self.plugin, "bot_token", None):
                rows = []
                for label, url in links[:3]:
                    low = label.lower()
                    if "github.com" in url and "/pull/" in url:
                        btn_label = i18n.t(lang, "pr_btn")
                    elif "connect" in low or "oauth" in url or "install" in url:
                        btn_label = i18n.t(lang, "connect_btn")
                    elif "invite" in url or "join" in low:
                        btn_label = i18n.t(lang, "invite_btn")
                    elif len(label) < 40 and not label.startswith("http"):
                        btn_label = label
                    else:
                        btn_label = i18n.t(lang, "open_btn")
                    rows.append([cta.url_btn(btn_label, url)])
                if await cta.send(self.plugin, chat_id, text, rows):
                    event_context.prevent_default()
                    await self.plugin.ingest(
                        telegram_user_id=tg_id,
                        telegram_chat_id=chat_id,
                        direction="out",
                        text=text,
                        meta={
                            "event": "NormalMessageResponded",
                            "kind": "cta_buttons",
                            "urls": [u for _, u in links[:3]],
                            "funcs_called": list(event.funcs_called or []),
                            "lang": lang,
                            "group": str(getattr(event, "launcher_type", "") or "").startswith("group")
                            or str(event.launcher_id).startswith("-"),
                        },
                    )
                    return

            await self.plugin.ingest(
                telegram_user_id=tg_id,
                telegram_chat_id=chat_id,
                direction="out",
                text=text,
                meta={
                    "event": "NormalMessageResponded",
                    "funcs_called": list(event.funcs_called or []),
                    "lang": self.plugin.lang_for(tg_id),
                    "group": str(getattr(event, "launcher_type", "") or "").startswith("group")
                    or str(event.launcher_id).startswith("-"),
                },
            )
