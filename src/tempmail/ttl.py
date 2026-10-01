"""Parsing for the ``--ttl`` option: ``30m``, ``1h``, ``24h``, ``7d``, ``90s``."""

import re

from .errors import UsageError

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_PATTERN = re.compile(r"^(\d+)([smhd])$", re.IGNORECASE)

# A year is plenty for a *temporary* address, and the cap keeps expires_at well
# inside the range a 64-bit unix timestamp can represent.
MAX_TTL_SECONDS = 365 * 86400


def parse_ttl(value):
    """Return the TTL in seconds, or ``None`` when ``value`` is empty."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    match = _PATTERN.match(text)
    if not match:
        raise UsageError(
            "Invalid --ttl value: {0!r}".format(value),
            hint="Use a number followed by s, m, h or d. Examples: 30m, 1h, 24h, 7d",
        )

    amount = int(match.group(1))
    seconds = amount * _UNITS[match.group(2).lower()]
    if seconds <= 0:
        raise UsageError("--ttl must be greater than zero")
    if seconds > MAX_TTL_SECONDS:
        raise UsageError(
            "--ttl is too long (max {0}d)".format(MAX_TTL_SECONDS // 86400)
        )
    return seconds


def format_duration(seconds):
    """Render a second count back as a compact human string (``2h30m``)."""
    if seconds is None:
        return "never"
    if seconds <= 0:
        return "expired"

    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            parts.append("{0}{1}".format(seconds // size, unit))
            seconds %= size
    if not parts:
        parts.append("{0}s".format(seconds))
    return "".join(parts[:2])
