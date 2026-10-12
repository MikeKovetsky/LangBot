"""The cron report: a founder-fixed language wins, and a weekly report says so."""
import asyncio
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(
    0,
    str(pathlib.Path(__file__).resolve().parents[1] / "plugins" / "viewfy__viewfy-agent"),
)
import i18n  # noqa: E402
import outbox  # noqa: E402


def test_report_lang_accepts_the_founders_languages_only():
    assert i18n.report_lang("ru") == "ru"
    assert i18n.report_lang("uk") == "ua"
    assert i18n.report_lang("pt-BR") == "pt"
    assert i18n.report_lang("xx") is None
    assert i18n.report_lang(None) is None
    # Chat replies still collapse to en | ua | ru.
    assert i18n.normalize_lang("pt") == "en"
    assert "Portuguese" in i18n.prompt_lang_line("pt")


def test_weekly_facts_ask_for_a_weekly_report():
    msg = outbox._facts_user_message(
        "daily_digest", {"outcome": "weekly", "start": "2026-10-05", "day": "2026-10-11"}, "en"
    )
    assert "complete weekly Telegram report" in msg
    morning = outbox._facts_user_message("daily_digest", {"outcome": "morning"}, "en")
    assert "complete morning Telegram report" in morning


def test_deliver_uses_the_payload_language(monkeypatch):
    seen = {}

    async def rewrite(_plugin, _kind, _payload, *, lang="en"):
        seen["lang"] = lang
        return "ok"

    monkeypatch.setattr(outbox, "rewrite", rewrite)
    plugin = SimpleNamespace(lang_for=lambda _uid: "en", bot_token=None)
    item = {
        "telegram_user_id": "1",
        "telegram_chat_id": "1",
        "kind": "daily_digest",
        "payload": {"outcome": "morning", "lang": "es"},
    }
    try:
        asyncio.run(outbox.deliver(plugin, item))
    except Exception:
        pass  # no bot token / LangBot runtime here; the rewrite already ran
    assert seen["lang"] == "es"

    item["payload"] = {"outcome": "morning"}
    plugin = SimpleNamespace(lang_for=lambda _uid: "ru", bot_token=None)
    try:
        asyncio.run(outbox.deliver(plugin, item))
    except Exception:
        pass
    assert seen["lang"] == "ru"
