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


def test_marketer_voice_drops_founder_slang():
    import voice

    founder = outbox.rewrite_system(None)
    assert "young YC founder" in founder and "while you were shipping" in founder
    assert "go build" in founder

    seo = outbox.rewrite_system("marketer")
    assert "head of SEO" in seo
    assert "while you were shipping" not in seo and "close with go build" not in seo
    assert "<MORNING>" not in seo and "<WEEKLY>" not in seo and "<CLOSE>" not in seo
    assert outbox.rewrite_system("nonsense") == founder

    assert voice.chat_line("marketer", "DMarket").startswith("READER: this group belongs to DMarket")
    assert voice.chat_line(None, "DMarket") == ""


def test_voice_is_not_a_fact_to_paraphrase():
    msg = outbox._facts_user_message("daily_digest", {"outcome": "morning", "voice": "marketer"}, "en")
    assert "marketer" not in msg


def test_deliver_rewrites_in_the_payload_voice(monkeypatch):
    seen = {}

    async def invoke_llm(*, llm_model_uuid, messages, extra_args):
        seen["system"] = messages[0].content
        return SimpleNamespace(content="ok")

    plugin = SimpleNamespace(llm_model_uuid="m", invoke_llm=invoke_llm)
    try:
        asyncio.run(outbox.rewrite(plugin, "daily_digest", {"outcome": "morning", "voice": "marketer"}))
    except ModuleNotFoundError:
        import pytest

        pytest.skip("langbot_plugin is not installed here")
    assert "head of SEO" in seen["system"]
