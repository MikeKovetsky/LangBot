"""Generic gate: do not claim side effects without a write-capable tool call.

Tools mirror Viewfy agent scopes (write / dynamic can mutate; read never does).
On a final Telegram reply that claims a mutation without such a tool, replace the
lie with a short decline.

Two rules keep this from firing on honest replies:

- Only mutation verbs count. "Let me read these and pick the best" is real work
  the read tools perform; a bare "one sec" is not a claim to have done anything.
- `funcs_called` must be the union over the whole user turn, not one chunk. The
  terminal message never carries tool_calls, and the tool that satisfies the
  promise usually ran a round or two earlier.
"""
from __future__ import annotations

import re
from typing import Iterable

try:
    # Generated from the agent registry's scopes, so a tool that grows write
    # actions cannot leave this gate refusing work it can now really do.
    from capabilities import READ_ONLY, WRITE_CAPABLE
except ImportError:  # generator never ran
    # Fail open. An unknown capability must not turn a truthful "published it"
    # into a refusal in the founder's chat; the worst case is the old behaviour.
    READ_ONLY: frozenset[str] = frozenset()
    WRITE_CAPABLE = None

# Claims of performing a mutation now. Reading, reviewing, and picking are not
# mutations, and "секунду / one sec" on its own claims nothing.
_PROMISE_RE = re.compile(
    r"(?:"
    # EN
    r"\b(?:i(?:'m| am)|i'll|i will)\s+(?:just\s+|now\s+)?(?:"
    r"writ(?:e|ing)|draft(?:ing)?|publish(?:ing)?|creat(?:e|ing)|send(?:ing)?|"
    r"post(?:ing)?|ship(?:ping)?|queu(?:e|ing)|deploy(?:ing)?|launch(?:ing)?|"
    r"approv(?:e|ing)|reject(?:ing)?|fix(?:ing)?|start(?:ing)?|runn?(?:ing)?"
    r")\b"
    # "opening" only counts for things we actually open.
    r"|\b(?:i(?:'m| am)|i'll|i will)\s+open(?:ing)?\s+(?:[\w-]+\s+){0,3}"
    r"(?:pr\b|pull request|issue|ticket)"
    # UA
    r"|\b(?:пишу|напишу|запускаю|запущу|створюю|створю|відправляю|відправлю|"
    r"публікую|опублікую|апрувлю|заапрувлю|реджектну)\b"
    # RU
    r"|\b(?:пишу|напишу|запускаю|запущу|создаю|создам|отправляю|отправлю|"
    r"публикую|опубликую|апрувлю)\b"
    r")",
    re.IGNORECASE,
)

# Soft suggestions / topic ideas — not a claim of doing the work.
_SUGGEST_RE = re.compile(
    r"(?:"
    r"\b(?:you (?:could|can|should)|we could|ideas?|outline|topics?|можно написати|"
    r"про що|що зайде|резонує|можу накидати)\b"
    r")",
    re.IGNORECASE,
)


def capability_line() -> str:
    if not WRITE_CAPABLE:
        return ""
    writable = ", ".join(sorted(WRITE_CAPABLE))
    readonly = ", ".join(sorted(READ_ONLY))
    return (
        "Tool capabilities: "
        f"writable/side-effect tools = [{writable}]; "
        f"read-only = [{readonly}]. "
        "Never claim you started, finished, or will perform a side effect unless you "
        "called a writable tool that can do that side effect and it succeeded. "
        "If no listed tool can do it, decline in one short line. "
        "Read-only tools never count as doing the work. Reading, reviewing, and "
        "comparing what the read tools return is work you can do — just say what you found."
    )


def is_side_effect_promise(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 800:
        # Long outlines are not "I'm writing now" promises.
        return False
    if not _PROMISE_RE.search(t):
        return False
    # "можу накидати outline" / topic lists with a soft CTA — allow.
    if _SUGGEST_RE.search(t) and not re.search(
        r"(?i)\b(?:пишу|i(?:'m| am) writing)\b", t
    ):
        return False
    return True


def called_write_capable(funcs: Iterable[str] | None) -> bool:
    if WRITE_CAPABLE is None:
        return True  # capabilities unknown — never call a claim a lie on a guess
    names = {str(f).strip() for f in (funcs or []) if str(f).strip()}
    return bool(names & WRITE_CAPABLE)


def should_decline(text: str, funcs_called: Iterable[str] | None) -> bool:
    """`funcs_called` must span the whole user turn — see module docstring."""
    if not is_side_effect_promise(text):
        return False
    return not called_write_capable(funcs_called)


DECLINE = {
    "en": (
        "I can't do that from chat — there's no tool that performs that action. "
        "Ask for something I can run with the tools I have, or do it in the dashboard."
    ),
    "ua": (
        "З чату це не зроблю — немає інструмента, який виконує таку дію. "
        "Попроси те, що я можу запустити тулами, або зроби в дашборді."
    ),
    "ru": (
        "Из чата это не сделаю — нет инструмента, который выполняет такое действие. "
        "Попроси то, что я могу запустить тулами, или сделай в дашборде."
    ),
}


def decline_text(lang: str) -> str:
    return DECLINE.get(lang) or DECLINE["en"]
