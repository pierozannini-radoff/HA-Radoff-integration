"""Hold our own identifiers back from text this integration did not write."""

from __future__ import annotations

import re

# What a redacted value reads as. The same marker `async_redact_data` writes,
# so a diagnostics dump never shows two spellings of the same thing.
REDACTED = "**REDACTED**"

# Two shapes worth recognising without knowing the instance they belong to: a
# domain id is a UUID, and a username is an email address. They cover the
# values an error body or an exception message can carry from an installation
# this process has never seen.
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[^\s<>()\[\]{}'\"]+@[^\s<>()\[\]{}'\",;]+\.[A-Za-z]{2,}")

# Below this length a value is too short to be replaced without mangling
# every sentence it appears inside. Nothing this integration holds is that
# short, so the guard only ever fires on a malformed one.
_MIN_VALUE_LENGTH = 4

# How much of a serial a log line may carry: enough to tell two devices of
# one installation apart, not enough to name one.
_SERIAL_TAIL = 4


def scrub(text: str, *values: str | None) -> str:
    """
    Return `text` with the given values, any UUID and any email address hidden.

    `values` are the fields this instance knows - its domain prefix, its
    username - and are what makes the result depend on our own data rather
    than on the wording the backend chose.
    """
    if not text:
        return text

    for value in values:
        if not value or len(value) < _MIN_VALUE_LENGTH:
            continue
        text = re.sub(re.escape(value), REDACTED, text, flags=re.IGNORECASE)

    text = _UUID_RE.sub(REDACTED, text)
    return _EMAIL_RE.sub(REDACTED, text)


def short_serial(serial: str | None) -> str:
    """
    Return the last four characters of a serial, which identify no device.

    Enough to tell two lines of one log apart, which is all a log needs it
    for; a serial with nothing left to drop is held back whole.
    """
    if not serial or len(serial) <= _SERIAL_TAIL:
        return REDACTED
    return f"...{serial[-_SERIAL_TAIL:]}"
