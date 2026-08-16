"""Telegram inline CTA helpers (URL + callback buttons)."""
from __future__ import annotations

import html
import logging
import re
from typing import Any

log = logging.getLogger("viewfy_agent.cta")

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"(?<![\(\[])(https?://[^\s<>\]\)]+)")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def fences_balanced(text: str) -> bool:
    """False while a streamed reply still has an open ``` fence."""
    return (text or "").count("```") % 2 == 0


def outside_fences(text: str) -> str:
    """Strip complete fenced blocks; empty if a fence is still open."""
    if not fences_balanced(text):
        return ""
    return _FENCE_RE.sub("", text or "")


def _html_inline(s: str) -> str:
    """Escape then apply markdown-ish link/bold on a non-code segment."""
    parts: list[str] = []
    pos = 0
    for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)]+)\)|\*\*(.+?)\*\*", s):
        parts.append(html.escape(s[pos : m.start()]))
        if m.group(1) is not None:
            parts.append(
                f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'
            )
        else:
            parts.append(f"<b>{html.escape(m.group(3))}</b>")
        pos = m.end()
    parts.append(html.escape(s[pos:]))
    return "".join(parts)


def html_from_markdownish(text: str) -> str:
    """Best-effort HTML for Telegram parse_mode=HTML (links + pre + bold)."""
    out: list[str] = []
    pos = 0
    for m in re.finditer(r"```(?:\w+)?\n(.*?)```", text or "", flags=re.DOTALL):
        out.append(_html_inline(text[pos : m.start()]))
        out.append(f"<pre>{html.escape(m.group(1))}</pre>")
        pos = m.end()
    out.append(_html_inline((text or "")[pos:]))
    return "".join(out)

# Tools that return a CTA url in data.
TOOL_CTA = {
    "connect": None,  # label from platform / purpose
    "inbox": "connect_cf_inbox_btn",  # attach → Cloudflare OAuth
    "form_pr": "pr_btn",
    "product_invite": "invite_btn",
    "product_members": "invite_btn",
}


def url_btn(text: str, url: str, *, primary: bool = True) -> dict[str, Any]:
    btn: dict[str, Any] = {"text": text[:64], "url": url}
    if primary:
        btn["style"] = "primary"
    return btn


def cb_btn(text: str, data: str, *, style: str | None = None) -> dict[str, Any]:
    btn: dict[str, Any] = {"text": text[:64], "callback_data": data[:64]}
    if style:
        btn["style"] = style
    return btn


def keyboard(rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def _collapse_blank_lines(text: str) -> str:
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    cleaned: list[str] = []
    blank = False
    for ln in lines:
        if not ln.strip():
            if not blank and cleaned:
                cleaned.append("")
            blank = True
            continue
        blank = False
        cleaned.append(ln)
    return "\n".join(cleaned).strip()


def strip_urls(text: str) -> str:
    """Remove markdown links and bare https URLs from copy (buttons carry them).

    Fenced draft/code bodies stay literal. An Open-thread button used to run this
    over the whole card and delete `[Viewfy](https://viewfy.ai?ref=…)` from the
    quoted draft, which is the text the founder approves.
    """
    if not text:
        return ""
    if not fences_balanced(text):
        return text
    parts: list[str] = []
    pos = 0
    for m in _FENCE_RE.finditer(text):
        outside = _MD_LINK_RE.sub("", text[pos : m.start()])
        parts.append(_BARE_URL_RE.sub("", outside))
        parts.append(m.group(0))
        pos = m.end()
    tail = _MD_LINK_RE.sub("", text[pos:])
    parts.append(_BARE_URL_RE.sub("", tail))
    return _collapse_blank_lines("".join(parts))


def extract_links(text: str) -> list[tuple[str, str]]:
    """(label, url) from markdown links then bare urls (deduped)."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _MD_LINK_RE.finditer(text or ""):
        label, url = m.group(1).strip(), m.group(2).strip()
        if url.startswith("https://") and url not in seen:
            seen.add(url)
            found.append((label or url, url))
    for m in _BARE_URL_RE.finditer(text or ""):
        url = m.group(1).rstrip(".,);]")
        if url.startswith("https://") and url not in seen:
            seen.add(url)
            found.append((url.split("/")[2] if "://" in url else url, url))
    return found


def label_for_tool(tool: str, data: dict[str, Any], lang: str) -> str:
    import i18n

    if tool == "connect":
        plat = (data.get("platform") or "").lower()
        if plat == "cloudflare" and (data.get("purpose") or "").lower() == "inbox":
            return i18n.t(lang, "connect_cf_inbox_btn")
        key = {"github": "connect_github_btn", "x": "connect_x_btn", "cloudflare": "connect_cf_btn"}.get(
            plat, "connect_btn"
        )
        return i18n.t(lang, key)
    if tool == "inbox":
        return i18n.t(lang, "connect_cf_inbox_btn")
    key = TOOL_CTA.get(tool) or "open_btn"
    return i18n.t(lang, key)


def cta_from_tool(tool: str, payload: dict[str, Any]) -> tuple[str, str] | None:
    """Return (label_key_or_resolved later, url) if this tool result should get a button."""
    if tool not in TOOL_CTA:
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    url = (data.get("url") or data.get("button_url") or "").strip()
    if not url.startswith("https://"):
        return None
    if tool == "form_pr" and "github.com" not in url:
        return None
    # inbox: only attach CTA for custom-domain OAuth (action=attach)
    if tool == "inbox" and (data.get("purpose") or "") != "inbox":
        return None
    return (tool, url)


async def send(
    plugin,
    chat_id: str,
    text: str,
    buttons: list[list[dict[str, Any]]],
    *,
    parse_mode: str | None = "HTML",
) -> bool:
    """Send copy + inline keyboard. Strips https from text when buttons carry URLs.

    Defaults to HTML so **bold** / fenced drafts render (same path as outbox).
    """
    if not getattr(plugin, "bot_token", None):
        return False
    body_text = strip_urls(text) if any("url" in b for row in buttons for b in row) else (text or "").strip()
    if not body_text:
        body_text = "👇"
    chat = str(chat_id).split("#", 1)[0]
    markup = keyboard(buttons)
    payload_text = (
        html_from_markdownish(body_text)[:4000]
        if parse_mode == "HTML"
        else body_text[:4000]
    )
    body: dict[str, Any] = {
        "chat_id": chat,
        "text": payload_text,
        "disable_web_page_preview": True,
        "link_preview_options": {"is_disabled": True},
        "reply_markup": markup,
    }
    if parse_mode:
        body["parse_mode"] = parse_mode
    out = await plugin._tg("sendMessage", body)
    if out.get("ok"):
        return True
    # Bad HTML entities → retry plain so the CTA still lands.
    if parse_mode:
        log.warning(
            "CTA %s send failed (%s); retrying plain",
            parse_mode,
            out.get("description"),
        )
        plain = {
            "chat_id": chat,
            "text": body_text[:4000],
            "disable_web_page_preview": True,
            "link_preview_options": {"is_disabled": True},
            "reply_markup": markup,
        }
        out = await plugin._tg("sendMessage", plain)
        if out.get("ok"):
            return True
    log.warning("CTA send failed: %s", out.get("description"))
    return False
