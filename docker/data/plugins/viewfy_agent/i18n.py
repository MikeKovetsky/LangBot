"""EN / UA / RU copy for Viewfy Telegram agent (LangBot plugin)."""
from __future__ import annotations

import re

Lang = str  # en | ua | ru

_LC_RE = re.compile(r"\|lc=([A-Za-z]{2,8}(?:-[A-Za-z0-9]+)?)(?:\||$)")
# Ukrainian-only letters; Russian-only letters (rough but good enough for chat).
_UA_RE = re.compile(r"[іІїЇєЄґҐ]")
_RU_RE = re.compile(r"[ыЫэЭъЪёЁ]")
_CYR_RE = re.compile(r"[\u0400-\u04FF]")

LANG_NAMES = {"en": "English", "ua": "Ukrainian", "ru": "Russian"}


def normalize_lang(raw: str | None) -> Lang:
    if not isinstance(raw, str) or not raw.strip():
        return "en"
    primary = raw.strip().lower().replace("_", "-").split("-", 1)[0]
    if primary in ("uk", "ua"):
        return "ua"
    if primary == "ru":
        return "ru"
    if primary == "en":
        return "en"
    return "en"


def lang_from_remark(remark: str | None) -> Lang | None:
    """Return normalized lang from |lc=xx, or None if no lc marker."""
    if not remark:
        return None
    m = _LC_RE.search(str(remark))
    if not m:
        return None
    return normalize_lang(m.group(1))


def lang_from_text(text: str | None) -> Lang | None:
    """Infer ua/ru from message script. None if no signal."""
    if not text or not _CYR_RE.search(text):
        return None
    if _UA_RE.search(text):
        return "ua"
    if _RU_RE.search(text):
        return "ru"
    # Shared Cyrillic (e.g. вюфай) — prefer Ukrainian for Viewfy dogfood.
    return "ua"


def lang_from_event(event) -> Lang | None:
    """Pull language_code from Friend.remark or GroupMember.special_title (|lc=)."""
    try:
        sender = getattr(getattr(event, "message_event", None), "sender", None)
        if sender is None:
            return None
        for attr in ("remark", "special_title", "member_name", "nickname"):
            raw = getattr(sender, attr, None)
            got = lang_from_remark(raw)
            if got:
                return got
        return None
    except Exception:
        return None


def resolve_lang(
    *,
    event=None,
    text: str | None = None,
    previous: Lang | None = None,
) -> Lang:
    """Client lc > message script > previous memory > en."""
    from_event = lang_from_event(event) if event is not None else None
    if from_event and from_event != "en":
        return from_event
    from_text = lang_from_text(text)
    if from_text:
        return from_text
    if from_event:
        return from_event
    if previous in ("ua", "ru", "en"):
        return previous  # type: ignore[return-value]
    return "en"


def t(lang: Lang, key: str) -> str:
    table = STRINGS.get(lang) or STRINGS["en"]
    return table.get(key) or STRINGS["en"][key]


def warm(lang: Lang) -> str:
    return t(lang, "warm")


def connect_btn(lang: Lang) -> str:
    return t(lang, "connect_btn")


def pr_btn(lang: Lang) -> str:
    return t(lang, "pr_btn")


def prepare_fail(lang: Lang, err: str) -> str:
    return t(lang, "prepare_fail").format(err=err)


def not_linked(lang: Lang, url: str = "") -> str:
    # url kept for call-site compat; copy uses the Connect button, not a pasted link.
    _ = url
    return t(lang, "not_linked")


def not_linked_nostart(lang: Lang) -> str:
    return t(lang, "not_linked_nostart")


def prompt_lang_line(lang: Lang) -> str:
    name = LANG_NAMES.get(lang, "English")
    return (
        f"LANGUAGE LOCK: reply entirely in {name} ({lang}). "
        f"Every line of every reply — greetings, status, metrics, lists, CTAs, errors — "
        f"must be {name}. Do not switch to English mid-thread. "
        f"Keep product names and URLs unchanged; translate labels around them."
    )


STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "warm": (
            "Hey, I'm Viewfy 💙\n\n"
            "I get you users while you ship: SEO/GEO, audits, fix PRs, blog, ads.\n\n"
            "Tap Connect Viewfy to link your account, then ask me what's next."
        ),
        "connect_btn": "💙 Connect Viewfy",
        "connect_github_btn": "Connect GitHub",
        "connect_x_btn": "Connect X",
        "connect_cf_btn": "Connect Cloudflare",
        "connect_cf_inbox_btn": "Connect Cloudflare for inbox",
        "pr_btn": "Open PR",
        "post_btn": "Read the post",
        "invite_btn": "Join product",
        "thread_btn": "Open thread",
        "open_btn": "Open",
        "approve_btn": "Approve",
        "reject_btn": "Reject",
        "review_drafts_btn": "Review drafts",
        "tap_below": "👇",
        "prepare_fail": (
            "Couldn't create a connect link right now. "
            "Try again in a minute. ({err})"
        ),
        "not_linked": (
            "Your Telegram is not linked to Viewfy yet.\n\n"
            "Tap Connect Viewfy below, then try again."
        ),
        "not_linked_nostart": (
            "Your Telegram is not linked to Viewfy yet. "
            "Send /start and tap Connect Viewfy."
        ),
        "tool_payload_too_large": (
            "Those full emails are too chunky for one Telegram reply. "
            "Name one site (like smash.vc) and I'll paste that draft."
        ),
    },
    "ua": {
        "warm": (
            "Привіт, я Viewfy 💙\n\n"
            "Я приводжу користувачів, поки ти шипиш: SEO/GEO, аудити, фікс-PR, блог, реклама.\n\n"
            "Натисни «Підключити Viewfy», щоб привʼязати акаунт — і питай, що далі."
        ),
        "connect_btn": "💙 Підключити Viewfy",
        "connect_github_btn": "Підключити GitHub",
        "connect_x_btn": "Підключити X",
        "connect_cf_btn": "Підключити Cloudflare",
        "connect_cf_inbox_btn": "Cloudflare для inbox",
        "pr_btn": "Відкрити PR",
        "post_btn": "Читати пост",
        "invite_btn": "Приєднатись",
        "thread_btn": "Відкрити тред",
        "open_btn": "Відкрити",
        "approve_btn": "Approve",
        "reject_btn": "Reject",
        "review_drafts_btn": "Переглянути драфти",
        "tap_below": "👇",
        "prepare_fail": (
            "Не вдалося створити посилання зараз. "
            "Спробуй ще раз за хвилину. ({err})"
        ),
        "not_linked": (
            "Telegram ще не привʼязано до Viewfy.\n\n"
            "Натисни «Підключити Viewfy» нижче і спробуй знову."
        ),
        "not_linked_nostart": (
            "Telegram ще не привʼязано до Viewfy. "
            "Надішли /start і натисни «Підключити Viewfy»."
        ),
        "tool_payload_too_large": (
            "Усі повні імейли разом не влазять в один меседж. "
            "Назви один сайт (типу smash.vc) — скину той драфт."
        ),
    },
    "ru": {
        "warm": (
            "Привет, я Viewfy 💙\n\n"
            "Я привожу пользователей, пока ты шипишь: SEO/GEO, аудиты, фикс-PR, блог, реклама.\n\n"
            "Нажми «Подключить Viewfy», чтобы привязать аккаунт — и спрашивай, что дальше."
        ),
        "connect_btn": "💙 Подключить Viewfy",
        "connect_github_btn": "Подключить GitHub",
        "connect_x_btn": "Подключить X",
        "connect_cf_btn": "Подключить Cloudflare",
        "connect_cf_inbox_btn": "Cloudflare для inbox",
        "pr_btn": "Открыть PR",
        "post_btn": "Читать пост",
        "invite_btn": "Присоединиться",
        "thread_btn": "Открыть тред",
        "open_btn": "Открыть",
        "approve_btn": "Approve",
        "reject_btn": "Reject",
        "review_drafts_btn": "Посмотреть драфты",
        "tap_below": "👇",
        "prepare_fail": (
            "Не удалось создать ссылку сейчас. "
            "Попробуй ещё раз через минуту. ({err})"
        ),
        "not_linked": (
            "Telegram ещё не привязан к Viewfy.\n\n"
            "Нажми «Подключить Viewfy» ниже и попробуй снова."
        ),
        "not_linked_nostart": (
            "Telegram ещё не привязан к Viewfy. "
            "Отправь /start и нажми «Подключить Viewfy»."
        ),
        "tool_payload_too_large": (
            "Все полные имейлы разом не влезают в одно сообщение. "
            "Назови один сайт (типа smash.vc) — скину тот драфт."
        ),
    },
}
