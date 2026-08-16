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
            f"{len(catalog)} outreach draft(s) listed (subjects only; bodies omitted — too large). "
            f"Domains: {domain_hint}. Use this catalog for status/counts. "
            f"Only if the founder asked to read a draft, call again with domain=<one domain> "
            f"for full subject+body. Do not invent email text. Do not volunteer the "
            f"payload-limit explanation unprompted."
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
            f"({tool_name}). Narrow with one domain / one id if the founder asked "
            f"for full detail. Do not volunteer a payload-limit explanation unprompted."
        ).strip(),
        "data": {
            "truncated": True,
            "error": "payload_too_large",
            "tool": tool_name,
        },
        "error": "payload_too_large",
    }


def _slim_scout_queue(out: dict[str, Any]) -> dict[str, Any]:
    """Keep target_url / permalink; drop draft bodies that blow the tool budget."""
    data = out.get("data") if isinstance(out.get("data"), dict) else {}
    items = data.get("actions") if isinstance(data.get("actions"), list) else []
    catalog: list[dict[str, Any]] = []
    for r in items:
        if not isinstance(r, dict):
            continue
        catalog.append(
            {
                "id": r.get("id"),
                "product_id": r.get("product_id"),
                "channel": r.get("channel"),
                "channel_label": r.get("channel_label"),
                "kind": r.get("kind"),
                "status": r.get("status"),
                "target_url": r.get("target_url"),
                "target_title": r.get("target_title"),
                "permalink": r.get("permalink"),
                "posted_at": r.get("posted_at"),
                "reason": r.get("reason"),
                "created_at": r.get("created_at"),
            }
        )
    n = len(catalog)
    with_url = sum(1 for c in catalog if (c.get("target_url") or "").strip())
    return {
        "summary": (
            f"{n} scout action(s); bodies omitted (too large). "
            f"{with_url} have target_url. Use target_url for the destination thread; "
            f"permalink is only set after post. Paste target_url when the founder asks "
            f"for the thread link. Do not volunteer a payload-limit explanation unprompted."
        ),
        "data": {
            "truncated": True,
            "error": "payload_too_large",
            "actions": catalog,
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
    elif tool_name in ("scout_queue", "roam_queue") and isinstance(data.get("actions"), list):
        slim = _slim_scout_queue(out)
    else:
        slim = _slim_generic(out, tool_name)

    if len(_dumps(slim)) <= TOOL_JSON_MAX:
        return slim, True

    # Last resort: tiny error object.
    return {
        "summary": (
            "Result was too large for one Telegram tool reply. "
            "Ask for one domain or one id only if the founder wants full detail."
        ),
        "data": {"truncated": True, "error": "payload_too_large", "tool": tool_name},
        "error": "payload_too_large",
    }, True


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
        # A name or domain in place of a product id resolves server-side in
        # run_tool, which /mcp shares — nothing to translate here.
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

        return await _send_tool_cta(plugin, tool_name, _dumps(fitted), chat_id, tg_id)
    finally:
        pulse.cancel()
        try:
            await pulse
        except asyncio.CancelledError:
            pass
        # Keep typing alive until the next LLM round (PromptPreProcessing) or final stop.
