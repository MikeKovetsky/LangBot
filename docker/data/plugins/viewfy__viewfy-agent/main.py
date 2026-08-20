from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langbot_plugin.api.definition.plugin import BasePlugin

# Plugin dir is not a package; load sibling i18n.py explicitly.
import sys as _sys

_PLUGIN_DIR = str(pathlib.Path(__file__).resolve().parent)
if _PLUGIN_DIR not in _sys.path:
    _sys.path.insert(0, _PLUGIN_DIR)
import i18n  # noqa: E402

log = logging.getLogger("viewfy_agent")

# Telegram chat actions expire ~5s; refresh under that. Idle stops the loop.
_TYPING_REFRESH_SEC = 4.0
_TYPING_IDLE_SEC = 8.0

# English default kept for imports/tests; prefer i18n.warm(lang).
WARM = i18n.warm("en")

# Preferred emoji → role for stickers in the `viewfy` pack.
ROLE_EMOJIS: dict[str, tuple[str, ...]] = {
    "wave": ("😜", "😊"),
    "happy": ("😊", "😜"),
    "sad": ("😒",),
    "social": ("🔊",),
}


def _file_settings() -> dict[str, Any]:
    path = pathlib.Path(__file__).resolve().parent / "settings.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        log.exception("failed reading settings.json")
        return {}


def _short_name(name: str | None, domain: str | None) -> str:
    """"Viewfy | He gets you users while you ship" -> "Viewfy (viewfy.ai)"."""
    label = re.split(r"[|:—–]| - ", (name or "").strip(), maxsplit=1)[0].strip()
    label = label[:40].strip()
    dom = (domain or "").strip()
    if not label:
        return dom
    return f"{label} ({dom})" if dom else label


class ViewfyAgentPlugin(BasePlugin):
    async def initialize(self) -> None:
        cfg = {**_file_settings(), **(self.get_config() or {})}
        self.api_base = (cfg.get("viewfy_api_base") or "http://host.docker.internal:9000").rstrip("/")
        self.secret = (cfg.get("telegram_agent_secret") or "").strip()
        self.bot_token = (cfg.get("telegram_bot_token") or "").strip()
        self.sticker_set = (cfg.get("sticker_set") or "viewfy").strip()
        self.bot_uuid = (cfg.get("bot_uuid") or "").strip()
        self.llm_model_uuid = (cfg.get("llm_model_uuid") or "").strip()
        self.stickers: dict[str, str] = {}  # emoji -> file_id
        self.sticker_order: list[str] = []
        self.user_lang: dict[str, str] = {}  # telegram_user_id -> en|ua|ru
        self.user_tz: dict[str, str] = {}  # telegram_user_id -> IANA zone
        self._lang_path = pathlib.Path(__file__).resolve().parent / "data" / "user_lang.json"
        self._load_user_lang()
        self._tz_path = pathlib.Path(__file__).resolve().parent / "data" / "user_tz.json"
        self._load_user_tz()
        self._typing_until: dict[str, float] = {}  # chat_id -> monotonic deadline
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._turn: dict[str, dict[str, Any]] = {}  # chat_id -> side-effect gate state
        self._anchor: dict[str, tuple[float, list[dict[str, Any]]]] = {}  # tg_id -> (deadline, products)
        self._outbox_task: asyncio.Task | None = None
        if self.bot_token and self.sticker_set:
            await self._load_stickers()
        if self.bot_uuid and self.llm_model_uuid and self.secret:
            import outbox as _outbox

            self._outbox_task = asyncio.create_task(_outbox.loop(self))
        else:
            log.warning(
                "outbox poller off (need bot_uuid, llm_model_uuid, secret); "
                "bot_uuid_set=%s llm_set=%s",
                bool(self.bot_uuid),
                bool(self.llm_model_uuid),
            )
        log.info(
            "viewfy_agent ready base=%s secret_set=%s bot_token_set=%s stickers=%d set=%s outbox=%s",
            self.api_base,
            bool(self.secret),
            bool(self.bot_token),
            len(self.stickers),
            self.sticker_set,
            bool(self._outbox_task),
        )

    def _load_user_lang(self) -> None:
        try:
            if self._lang_path.is_file():
                raw = json.loads(self._lang_path.read_text())
                if isinstance(raw, dict):
                    self.user_lang = {
                        str(k): i18n.normalize_lang(v)
                        for k, v in raw.items()
                        if v in ("en", "ua", "ru", "uk")
                    }
        except Exception:
            log.exception("load user_lang failed")

    def _save_user_lang(self) -> None:
        try:
            self._lang_path.parent.mkdir(parents=True, exist_ok=True)
            self._lang_path.write_text(json.dumps(self.user_lang, ensure_ascii=False, indent=0))
        except Exception:
            log.exception("save user_lang failed")

    def remember_lang(self, telegram_user_id: str, lang: str | None) -> str:
        """Store lang. Do not clobber ua/ru with a bare default en. Persist to disk."""
        key = str(telegram_user_id)
        prev = self.user_lang.get(key)
        if not lang:
            return prev or "en"
        resolved = i18n.normalize_lang(lang)
        if resolved == "en" and prev in ("ua", "ru"):
            return prev
        if self.user_lang.get(key) != resolved:
            self.user_lang[key] = resolved
            self._save_user_lang()
        return resolved

    def _load_user_tz(self) -> None:
        try:
            if self._tz_path.is_file():
                raw = json.loads(self._tz_path.read_text())
                if isinstance(raw, dict):
                    self.user_tz = {str(k): str(v) for k, v in raw.items() if v}
        except Exception:
            log.exception("load user_tz failed")

    def _save_user_tz(self) -> None:
        try:
            self._tz_path.parent.mkdir(parents=True, exist_ok=True)
            self._tz_path.write_text(json.dumps(self.user_tz, ensure_ascii=False, indent=0))
        except Exception:
            log.exception("save user_tz failed")

    def tz_for(self, telegram_user_id: str) -> str:
        """The founder's zone. Telegram never tells us, so it is set or defaulted."""
        return (
            self.user_tz.get(str(telegram_user_id))
            or os.getenv("VIEWFY_DEFAULT_TZ")
            or "UTC"
        )

    def remember_tz(self, telegram_user_id: str, tz: str) -> bool:
        name = (tz or "").strip()
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return False
        self.user_tz[str(telegram_user_id)] = name
        self._save_user_tz()
        return True

    def now_for(self, telegram_user_id: str) -> tuple[datetime, str]:
        """(local now, zone name). Falls back to UTC rather than guessing."""
        name = self.tz_for(telegram_user_id)
        try:
            return datetime.now(ZoneInfo(name)), name
        except Exception:
            return datetime.now(timezone.utc), "UTC"

    def lang_for(self, telegram_user_id: str) -> str:
        return self.user_lang.get(str(telegram_user_id), "en")

    def turn_start(self, chat_id: str | int | None) -> None:
        """New inbound message — reset what the side-effect gate knows about this chat.

        One reply is many NormalMessageResponded events (a stream chunk, a tool
        round). Tools called anywhere in the turn count, and the gate speaks once.
        """
        key = str(chat_id or "").split("#", 1)[0]
        if key:
            self._turn[key] = {"tools": set(), "declined": False}

    def set_quote(self, chat_id: str | int | None, quote: dict[str, Any] | None) -> None:
        """Stash the replied-to message for this chat's turn.

        PromptPreProcessing cannot see the message chain out-of-process, so the
        inbound handler parks the quote here and the prompt hook collects it.
        """
        key = str(chat_id or "").split("#", 1)[0]
        if key:
            self.turn_state(key)["quote"] = quote

    def get_quote(self, chat_id: str | int | None) -> dict[str, Any] | None:
        """Read-only: the prompt rebuilds once per tool round and needs it each time.
        turn_start clears it when the next inbound message opens a new turn."""
        key = str(chat_id or "").split("#", 1)[0]
        if not key:
            return None
        return self.turn_state(key).get("quote")

    def turn_state(self, chat_id: str | int | None) -> dict[str, Any]:
        key = str(chat_id or "").split("#", 1)[0]
        if not key:
            return {"tools": set(), "declined": False}
        return self._turn.setdefault(key, {"tools": set(), "declined": False})

    def start_typing(self, chat_id: str | int | None) -> None:
        """Show Telegram 'typing…' and keep refreshing until idle."""
        if not chat_id or not self.bot_token:
            return
        key = str(chat_id).split("#", 1)[0]
        self._typing_until[key] = time.monotonic() + _TYPING_IDLE_SEC
        task = self._typing_tasks.get(key)
        if task is None or task.done():
            self._typing_tasks[key] = asyncio.create_task(self._typing_loop(key))

    def stop_typing(self, chat_id: str | int | None) -> None:
        if not chat_id:
            return
        key = str(chat_id).split("#", 1)[0]
        self._typing_until.pop(key, None)
        task = self._typing_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    async def _typing_loop(self, chat_id: str) -> None:
        try:
            while time.monotonic() < self._typing_until.get(chat_id, 0):
                out = await self._tg(
                    "sendChatAction",
                    {"chat_id": chat_id, "action": "typing"},
                )
                if not out.get("ok"):
                    log.debug("sendChatAction typing failed: %s", out.get("description"))
                    break
                remaining = self._typing_until.get(chat_id, 0) - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(_TYPING_REFRESH_SEC, remaining))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("typing loop failed chat=%s", chat_id)
        finally:
            self._typing_tasks.pop(chat_id, None)
            self._typing_until.pop(chat_id, None)

    async def _tg(self, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.bot_token:
            return {"ok": False, "description": "no bot token"}
        api = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        data = None if body is None else json.dumps(body).encode()

        def do() -> dict[str, Any]:
            req = urllib.request.Request(
                api,
                data=data,
                headers={"Content-Type": "application/json"} if data else {},
                method="POST" if data is not None else "GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                err = e.read().decode(errors="replace")
                try:
                    return json.loads(err)
                except Exception:
                    return {"ok": False, "description": err or str(e)}
            except Exception as e:
                return {"ok": False, "description": str(e)}

        return await asyncio.to_thread(do)

    async def _load_stickers(self) -> None:
        out = await self._tg("getStickerSet", {"name": self.sticker_set})
        if not out.get("ok"):
            log.warning("getStickerSet %s failed: %s", self.sticker_set, out.get("description"))
            return
        stickers = (out.get("result") or {}).get("stickers") or []
        by_emoji: dict[str, str] = {}
        order: list[str] = []
        for s in stickers:
            eid = s.get("emoji") or ""
            fid = s.get("file_id") or ""
            if not fid:
                continue
            order.append(fid)
            if eid and eid not in by_emoji:
                by_emoji[eid] = fid
        self.stickers = by_emoji
        self.sticker_order = order
        log.info("loaded sticker set %s: %s", self.sticker_set, list(by_emoji.keys()))

    def sticker_file_id(self, role: str = "wave") -> str | None:
        for emoji in ROLE_EMOJIS.get(role, ()):
            if emoji in self.stickers:
                return self.stickers[emoji]
        if self.sticker_order:
            return self.sticker_order[0]
        return None

    async def send_sticker(self, chat_id: str, role: str = "wave") -> bool:
        file_id = self.sticker_file_id(role)
        if not file_id:
            return False
        out = await self._tg("sendSticker", {"chat_id": chat_id, "sticker": file_id})
        if not out.get("ok"):
            log.warning("sendSticker failed: %s", out.get("description"))
            # file_id can go stale after bot restart; reload once
            await self._load_stickers()
            file_id = self.sticker_file_id(role)
            if not file_id:
                return False
            out = await self._tg("sendSticker", {"chat_id": chat_id, "sticker": file_id})
        return bool(out.get("ok"))

    def _headers(self, bearer: str | None = None) -> dict[str, str]:
        # Cloudflare Browser Integrity Check returns 1010 for Python-urllib's
        # default UA; use a normal browser UA so prepare/me/agent calls land.
        h = {
            "Content-Type": "application/json",
            "X-Telegram-Agent-Secret": self.secret,
            "User-Agent": (
                "Mozilla/5.0 (compatible; ViewfyTelegramAgent/1.0; "
                "+https://viewfy.ai)"
            ),
            "Accept": "application/json",
        }
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"
        return h

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        bearer: str | None = None,
        query: dict | None = None,
    ) -> dict[str, Any]:
        url = f"{self.api_base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = None if body is None else json.dumps(body).encode()

        def do() -> dict[str, Any]:
            req = urllib.request.Request(url, data=data, headers=self._headers(bearer), method=method)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read().decode() or "{}"
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                err_body = e.read().decode(errors="replace")
                try:
                    parsed = json.loads(err_body)
                except Exception:
                    parsed = {"detail": err_body or str(e)}
                parsed["_http_status"] = e.code
                return parsed
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                # A client-side timeout says nothing about the server, which may
                # still finish and commit. Never report it as a plain failure.
                return {
                    "_http_status": 0,
                    "detail": (
                        "Request timed out or the connection dropped before a "
                        "response arrived. The change MAY still have been applied "
                        "on the server. Read the current state back before "
                        f"retrying or reporting failure. ({e})"
                    ),
                }

        return await asyncio.to_thread(do)

    async def prepare_link(self, telegram_user_id: str, lang: str | None = None) -> str:
        body: dict[str, Any] = {"telegram_user_id": str(telegram_user_id)}
        if lang:
            body["lang"] = i18n.normalize_lang(lang)
        out = await self._request(
            "POST",
            "/api/telegram/agent/prepare",
            body=body,
        )
        url = out.get("url")
        if not url:
            raise RuntimeError(out.get("detail") or "prepare failed")
        return str(url)

    async def start_reply(self, telegram_user_id: str, lang: str | None = None) -> tuple[str, str]:
        """Diegetic Connect offer (facts → LLM rewrite; URL appended from facts)."""
        import diegetic

        lang_n = self.remember_lang(telegram_user_id, lang or self.lang_for(telegram_user_id))
        facts = await diegetic.prepare_connect_facts(self, telegram_user_id=telegram_user_id, lang=lang_n)
        text = await diegetic.rewrite_offer(self, kind="connect_offer", facts=facts, lang=lang_n)
        return text, str(facts["url"])

    async def offer_product_invite(
        self,
        *,
        telegram_user_id: str,
        telegram_chat_id: str,
        lang: str | None = None,
    ) -> tuple[str, str]:
        """Mint product invite facts + diegetic rewrite for an unlinked speaker in a pinned group."""
        import diegetic

        lang_n = self.remember_lang(telegram_user_id, lang or self.lang_for(telegram_user_id))
        facts = await diegetic.mint_product_invite_facts(
            self,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
        )
        text = await diegetic.rewrite_offer(
            self, kind="product_invite", facts=facts, lang=lang_n
        )
        return text, str(facts["url"])

    async def offer_connect(
        self,
        *,
        telegram_user_id: str,
        lang: str | None = None,
    ) -> tuple[str, str]:
        import diegetic

        lang_n = self.remember_lang(telegram_user_id, lang or self.lang_for(telegram_user_id))
        facts = await diegetic.prepare_connect_facts(self, telegram_user_id=telegram_user_id, lang=lang_n)
        text = await diegetic.rewrite_offer(self, kind="connect_offer", facts=facts, lang=lang_n)
        return text, str(facts["url"])

    async def send_connect_message(
        self,
        chat_id: str,
        text: str,
        url: str | None = None,
        *,
        lang: str = "en",
        button_key: str = "connect_btn",
        sticker: bool = True,
    ) -> bool:
        """Sticker (optional) + copy + URL CTA button. Never leave a bare https in the body."""
        import cta

        if not self.bot_token:
            return False

        if sticker:
            await self.send_sticker(chat_id, "wave")

        if not url:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            url = next(
                (ln for ln in reversed(lines) if ln.startswith("http://") or ln.startswith("https://")),
                None,
            )

        if url and url.startswith("https://"):
            return await cta.send(
                self,
                chat_id,
                text,
                [[cta.url_btn(i18n.t(lang, button_key), url)]],
            )

        body = {
            "chat_id": chat_id,
            "text": (text or "")[:4000],
            "disable_web_page_preview": True,
            "link_preview_options": {"is_disabled": True},
        }
        out = await self._tg("sendMessage", body)
        if not out.get("ok"):
            log.warning("sendMessage failed: %s", out.get("description"))
            return False
        return True

    async def ingest(
        self,
        *,
        telegram_user_id: str,
        telegram_chat_id: str,
        direction: str,
        text: str,
        telegram_message_id: str | None = None,
        meta: dict | None = None,
    ) -> None:
        try:
            await self._request(
                "POST",
                "/api/telegram/agent/messages",
                body={
                    "telegram_user_id": str(telegram_user_id),
                    "telegram_chat_id": str(telegram_chat_id),
                    "direction": direction,
                    "text": text or "",
                    "telegram_message_id": telegram_message_id,
                    "meta": meta or {},
                },
            )
        except Exception:
            log.exception("ingest failed")

    async def me(self, telegram_user_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/api/telegram/agent/me",
            query={"telegram_user_id": str(telegram_user_id)},
        )

    async def products_of(self, telegram_user_id: str) -> list[dict[str, Any]]:
        """The founder's products, cached. Empty for unlinked users.

        Prompts fire once per tool round, so this must not hit the API each time.
        Unlinked goes through `me` first: call_agent sends a Connect message for
        them, which must never be a side effect of building a prompt.
        """
        tg_id = str(telegram_user_id or "").strip()
        if not tg_id:
            return []
        hit = self._anchor.get(tg_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]

        items: list[dict[str, Any]] = []
        try:
            me = await self.me(tg_id)
            if me.get("linked"):
                raw = await self.call_agent("products", {}, tg_id)
                got = (json.loads(raw).get("data") or {}).get("products") or []
                items = [p for p in got if isinstance(p, dict) and p.get("id")]
        except Exception:
            log.warning("product fetch failed for %s", tg_id, exc_info=True)

        # Cache failures briefly too, so a flaky API cannot stall every prompt.
        self._anchor[tg_id] = (time.monotonic() + (300 if items else 60), items)
        return items

    async def product_anchor(self, telegram_user_id: str) -> str:
        """Prompt line naming the founder's products, for DMs.

        A group gets its anchor from the pin. A DM had none, so a fresh session
        knew no product and asked the founder for a raw product_id.
        """
        named = [
            _short_name(p.get("name"), p.get("domain"))
            for p in await self.products_of(telegram_user_id)
            if p.get("name") or p.get("domain")
        ]
        if not named:
            return ""
        if len(named) == 1:
            line = (
                f"The founder has one product: {named[0]}. Use it for every product "
                "tool without asking which one."
            )
        else:
            line = (
                f"The founder's products: {', '.join(named[:8])}. Ask which one by "
                "name or domain when it is ambiguous."
            )
        return line + (
            " Never ask the founder for a product_id, post_id, action_id, or any other "
            "tool parameter name - those are internal. Pass the name or domain as "
            "product_id and it resolves; refer to products by name or domain in chat."
        )

    async def call_agent(
        self,
        tool: str,
        params: dict[str, Any],
        telegram_user_id: str,
        telegram_chat_id: str | None = None,
    ) -> str:
        lang = self.lang_for(telegram_user_id)
        me = await self.me(telegram_user_id)
        if not me.get("linked") or not me.get("access_token"):
            chat = str(telegram_chat_id or "").split("#", 1)[0]
            # Pinned group + unlinked → product invite facts (diegetic), not Connect-only.
            if chat.startswith("-"):
                try:
                    pin = await self._request(
                        "GET",
                        "/api/telegram/agent/group_pin",
                        query={"telegram_chat_id": chat},
                    )
                except Exception:
                    pin = {}
                if pin.get("pinned"):
                    try:
                        text, url = await self.offer_product_invite(
                            telegram_user_id=telegram_user_id,
                            telegram_chat_id=chat,
                            lang=lang,
                        )
                        if chat:
                            await self.send_connect_message(
                                chat, text, url, lang=lang, button_key="invite_btn", sticker=False
                            )
                        return json.dumps(
                            {
                                "summary": "Join-product button sent in Telegram. Do not paste the URL.",
                                "data": {"button_sent": True, "url": url},
                            },
                            ensure_ascii=False,
                        )
                    except Exception as e:
                        log.warning("product invite offer failed: %s", e)
            try:
                text, url = await self.offer_connect(telegram_user_id=telegram_user_id, lang=lang)
                if chat:
                    await self.send_connect_message(chat, text, url, lang=lang)
                return json.dumps(
                    {
                        "summary": "Connect Viewfy button sent in Telegram. Do not paste the URL.",
                        "data": {"button_sent": True, "url": url},
                    },
                    ensure_ascii=False,
                )
            except Exception:
                return i18n.not_linked_nostart(lang)
        body: dict[str, Any] = {"args": params or {}}
        if telegram_chat_id:
            body["telegram_chat_id"] = str(telegram_chat_id).split("#", 1)[0]
        out = await self._request(
            "POST",
            f"/api/agent/{tool}",
            body=body,
            bearer=str(me["access_token"]),
        )
        # Do not mid-cut JSON here — tool_call.agent slims oversized payloads and
        # sends a diegetic note in chat when bodies won't fit Telegram.
        return json.dumps(out, ensure_ascii=False)
