"""Shared LangBot tool → Viewfy /api/agent helper."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import cta
import i18n

log = logging.getLogger("viewfy_agent.tool_call")

# LangBot / model tool-result budget. Never return a mid-cut JSON string.
TOOL_JSON_MAX = 6000


def tg_ids(session) -> tuple[str, str]:
    """(telegram_user_id, telegram_chat_id)."""
    tg_id = str(session.sender_id or session.launcher_id)
    chat_id = str(session.launcher_id)
    return tg_id, chat_id


def _dumps(out: dict[str, Any]) -> str:
    return json.dumps(out, ensure_ascii=False)


def _slim_links_outreach(out: dict[str, Any]) -> dict[str, Any]:
    """Drop email bodies so a queue/outreach list fits the tool budget."""
    data = out.get("data") if isinstance(out.get("data"), dict) else {}
    items = data.get("outreach") if isinstance(data.get("outreach"), list) else []
    catalog: list[dict[str, Any]] = []
    for r in items:
        if not isinstance(r, dict):
            continue
        catalog.append(
            {
                "id": r.get("id"),
                "domain": r.get("domain"),
                "to": r.get("to"),
                "subject": r.get("subject"),
                "status": r.get("status"),
                "kind": r.get("kind"),
            }
        )
    domains = [c["domain"] for c in catalog if c.get("domain")]
    domain_hint = ", ".join(domains[:8]) if domains else "a domain from the list"
    return {
        "summary": (
            f"{len(catalog)} outreach draft(s) ready, but full bodies are too long for one "
            f"Telegram tool result. Call again with domain=<one of: {domain_hint}> to print "
            f"that full subject+body. Do not invent email text. A short note was already "
            f"sent to the founder in chat."
        ),
        "data": {
            "action": data.get("action") or "outreach",
            "truncated": True,
            "error": "payload_too_large",
            "outreach": catalog,
            "hint": "pass domain= to fetch one full email body",
        },
        "error": "payload_too_large",
    }


def _slim_generic(out: dict[str, Any], tool_name: str) -> dict[str, Any]:
    summary = (out.get("summary") or "").strip()
    if len(summary) > 1500:
        summary = summary[:1500].rstrip() + "…"
    return {
        "summary": (
            f"{summary} Result was too large for one Telegram tool reply "
            f"({tool_name}). Ask for a narrower slice (one domain / one id). "
            f"A short note was already sent to the founder in chat."
        ).strip(),
        "data": {
            "truncated": True,
            "error": "payload_too_large",
            "tool": tool_name,
        },
        "error": "payload_too_large",
    }


def _fit_tool_json(tool_name: str, out: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return (payload, oversized). Oversized payloads are slimmed, never mid-cut."""
    if len(_dumps(out)) <= TOOL_JSON_MAX:
        return out, False

    data = out.get("data") if isinstance(out.get("data"), dict) else {}
    action = (data.get("action") or "").lower()
    if tool_name == "links" and (
        action in ("outreach", "queue") or isinstance(data.get("outreach"), list)
    ):
        slim = _slim_links_outreach(out)
    else:
        slim = _slim_generic(out, tool_name)

    if len(_dumps(slim)) <= TOOL_JSON_MAX:
        return slim, True

    # Last resort: tiny error object.
    return {
        "summary": (
            "Result was too large for one Telegram tool reply. "
            "Ask for one domain or one id. A short note was sent in chat."
        ),
        "data": {"truncated": True, "error": "payload_too_large", "tool": tool_name},
        "error": "payload_too_large",
    }, True


async def _diegetic_too_large(
    plugin, tool_name: str, chat_id: str, tg_id: str
) -> None:
    lang = plugin.lang_for(tg_id)
    text = i18n.t(lang, "tool_payload_too_large")
    chat = str(chat_id).split("#", 1)[0]
    try:
        out = await plugin._tg(
            "sendMessage",
            {
                "chat_id": chat,
                "text": text[:4000],
                "disable_web_page_preview": True,
                "link_preview_options": {"is_disabled": True},
            },
        )
        if not out.get("ok"):
            log.warning("diegetic too_large send failed: %s", out.get("description"))
            return
        await plugin.ingest(
            telegram_user_id=str(tg_id),
            telegram_chat_id=chat,
            direction="out",
            text=text,
            meta={
                "kind": "tool_payload_too_large",
                "tool": tool_name,
                "via": "tool_call",
            },
        )
    except Exception:
        log.exception("diegetic too_large failed tool=%s", tool_name)


async def _send_tool_cta(plugin, tool_name: str, raw: str, chat_id: str, tg_id: str) -> str:
    """Attach inline URL buttons for tool CTAs; rewrite summary so the LLM does not paste URLs."""
    if not raw:
        return raw
    try:
        out = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(out, dict):
        return raw

    hit = cta.cta_from_tool(tool_name, out)
    if not hit:
        return raw
    _tool, url = hit
    data = out.get("data") if isinstance(out.get("data"), dict) else {}
    lang = plugin.lang_for(tg_id)
    label = cta.label_for_tool(tool_name, data, lang)

    # Short diegetic line; button carries the link.
    if tool_name == "form_pr" and data.get("number"):
        text = (
            f"PR #{data['number']} готовий 🎉" if lang == "ua"
            else f"PR #{data['number']} готов 🎉" if lang == "ru"
            else f"PR #{data['number']} is live 🎉"
        )
    elif tool_name == "connect":
        plat = (data.get("platform") or "account").lower()
        purpose = (data.get("purpose") or "").lower()
        if plat == "cloudflare" and purpose == "inbox":
            addr = data.get("address") or "hi@inbox"
            text = (
                f"Підключи inbox ({addr}) через Cloudflare — кнопка нижче." if lang == "ua"
                else f"Подключи inbox ({addr}) через Cloudflare — кнопка ниже." if lang == "ru"
                else f"Put the agent inbox on {addr} — tap Cloudflare below."
            )
        else:
            label = plat.capitalize() if plat != "x" else "X"
            text = (
                f"Підключи {label} кнопкою нижче." if lang == "ua"
                else f"Подключи {label} кнопкой ниже." if lang == "ru"
                else f"Connect {label} with the button below."
            )
    elif tool_name == "inbox":
        addr = data.get("address") or "hi@inbox"
        text = (
            f"Підключи inbox ({addr}) через Cloudflare — кнопка нижче." if lang == "ua"
            else f"Подключи inbox ({addr}) через Cloudflare — кнопка ниже." if lang == "ru"
            else f"Put the agent inbox on {addr} — tap Cloudflare below."
        )
    elif tool_name in ("product_invite", "product_members"):
        name = data.get("product_name") or "the product"
        text = (
            f"Інвайт на {name} — кнопка нижче." if lang == "ua"
            else f"Инвайт на {name} — кнопка ниже." if lang == "ru"
            else f"Invite for {name} — tap below."
        )
    else:
        text = i18n.t(lang, "tap_below")

    ok = await cta.send(plugin, chat_id, text, [[cta.url_btn(label, url)]])
    if ok:
        await plugin.ingest(
            telegram_user_id=str(tg_id),
            telegram_chat_id=str(chat_id).split("#", 1)[0],
            direction="out",
            text=f"{text}\n{url}",
            meta={"kind": "cta_button", "tool": tool_name, "url": url, "via": "tool_call"},
        )
        out["summary"] = (
            f"{out.get('summary') or ''} Button sent in Telegram ({label}). "
            "Do not paste the URL in chat."
        ).strip()
        out["data"] = {**data, "button_sent": True}
        fitted, _ = _fit_tool_json(tool_name, out)
        return _dumps(fitted)
    return raw


async def agent(plugin, tool_name: str, params: dict[str, Any] | None, session) -> str:
    tg_id, chat_id = tg_ids(session)
    plugin.start_typing(chat_id)

    async def _pulse() -> None:
        try:
            while True:
                await asyncio.sleep(3.5)
                plugin.start_typing(chat_id)
        except asyncio.CancelledError:
            raise

    pulse = asyncio.create_task(_pulse())
    try:
        raw = await plugin.call_agent(tool_name, params or {}, tg_id, chat_id)
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            # Non-JSON (e.g. not_linked plain text) — pass through, still budget-cap text.
            return (raw or "")[:TOOL_JSON_MAX]

        if not isinstance(parsed, dict):
            return (raw or "")[:TOOL_JSON_MAX]

        fitted, oversized = _fit_tool_json(tool_name, parsed)
        if oversized:
            log.warning(
                "tool result oversized tool=%s action=%s",
                tool_name,
                (fitted.get("data") or {}).get("action") if isinstance(fitted.get("data"), dict) else None,
            )
            await _diegetic_too_large(plugin, tool_name, chat_id, tg_id)

        return await _send_tool_cta(plugin, tool_name, _dumps(fitted), chat_id, tg_id)
    finally:
        pulse.cancel()
        try:
            await pulse
        except asyncio.CancelledError:
            pass
        plugin.start_typing(chat_id)
