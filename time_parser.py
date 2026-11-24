import datetime
import re


_DURATION_PATTERN = re.compile(
    r"(?P<amount>\d+)\s*(?P<unit>years?|yrs?|yr|y|months?|mo|mon|weeks?|w|days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)",
    re.IGNORECASE,
)
_PERMANENT_VALUES = {"p", "perm", "permanent"}
_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
    "mo": 30 * 86400,
    "mon": 30 * 86400,
    "month": 30 * 86400,
    "months": 30 * 86400,
    "y": 365 * 86400,
    "yr": 365 * 86400,
    "yrs": 365 * 86400,
    "year": 365 * 86400,
    "years": 365 * 86400,
}


def parse_duration(value: str) -> datetime.timedelta | None:
    """Parse a duration; return None for permanent and raise ValueError if invalid."""
    normalized = value.strip().lower()
    if normalized in _PERMANENT_VALUES:
        return None

    matches = list(_DURATION_PATTERN.finditer(normalized))
    if not matches:
        raise ValueError("Enter a duration such as 30s, 2 weeks, or p for permanent.")

    cursor = 0
    total_seconds = 0
    for match in matches:
        if normalized[cursor:match.start()].strip():
            raise ValueError("Duration contains an invalid value or unit.")
        amount = int(match.group("amount"))
        if amount <= 0:
            raise ValueError("Duration must be greater than zero.")
        total_seconds += amount * _UNIT_SECONDS[match.group("unit").lower()]
        cursor = match.end()

    if normalized[cursor:].strip():
        raise ValueError("Duration contains an invalid value or unit.")
    try:
        return datetime.timedelta(seconds=total_seconds)
    except OverflowError as error:
        raise ValueError("Duration is too large.") from error
