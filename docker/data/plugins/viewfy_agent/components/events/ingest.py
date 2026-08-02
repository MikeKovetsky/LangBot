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

FORMAT_LINE = (
    "Format replies in Markdown for Telegram: use **bold** and fenced code blocks for "
    "draft or post bodies. Do NOT paste CTA https links or markdown [label](url) for "
    "connect, invite, PR, or thread opens — Telegram attaches those as inline buttons "
    "from tool results / outbox. Never show raw UUIDs or internal action ids in chat "
    "(except when the user message already contains action_id= from an Approve/Reject button). "
    "Never invent a product URL. Call scrape only with a URL the founder typed in this chat; "
    "before creating a product, echo the exact URL and get a clear yes. Bare hey/@ pings are "
    "not consent to scrape. If unsure, ask. "
    "For users per day / site traffic / pageviews, call stats (Viewfy tracker). "
    "Never say you lack traffic metrics. Never ask for GA, Plausible, or Cloudflare Analytics. "
    "For inbox send: real subject, short body, low volume while warming (not blasts). "
    "Do not claim Gmail shows a brand avatar without CMC/VMC. "
    "Cloudflare is only for blog DNS/worker and inbox email — check blog/inbox/products "
    "status fields before claiming Cloudflare is not connected. "
    "If the founder message is 'approve action_id=...' or 'reject action_id=...', call "
    "roam_approve with that id and decision immediately (skip roam_queue). "
    "If they type approve/reject without an id, call roam_queue then roam_approve."
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
        if isinstance(component, platform_message.Plain):
            parts.append(component.text or "")
        elif hasattr(component, "text") and getattr(component, "text", None):
            parts.append(str(component.text))
    return "".join(parts).strip()


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
    lang = plugin.remember_lang(
        tg_id,
        i18n.resolve_lang(
            event=event,
            text=text,
            previous=plugin.lang_for(tg_id),
        ),
    )

    await plugin.ingest(
        telegram_user_id=tg_id,
        telegram_chat_id=chat_id,
        direction="in",
        text=text,
        telegram_message_id=_message_id(event.message_chain),
        meta={"event": event_name, "lang": lang, "group": is_group},
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

            prompts = list(event.default_prompt or [])
            if FORMAT_LINE not in _content_blob(prompts):
                prompts.append(provider_message.Message(role="system", content=FORMAT_LINE))
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
            # Extend typing briefly in case another tool/LLM round follows.
            self.plugin.start_typing(chat_id)

            # Tool-call tracker lines ("Call foo...") — leave alone.
            if text.startswith("Call ") and "..." in text:
                return

            # Lift webpage CTAs out of the body into Telegram inline buttons.
            # Skip fenced draft/code bodies for bare URLs; keep markdown links.
            outside = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
            links = cta.extract_links(outside)
            if links and getattr(self.plugin, "bot_token", None):
                lang = self.plugin.lang_for(tg_id)
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
