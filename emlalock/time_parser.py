import re

_UNIT_SECONDS = {
    "w": 604800, "week": 604800, "weeks": 604800,
    "d": 86400, "day": 86400, "days": 86400,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
}
_TOKEN = re.compile(r"(\d+)\s*([a-zA-Z]+)")


def parse_duration(text: str) -> int:
    """Convert explicit week/day/hour/minute/second components to seconds."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Tell me an amount and unit, such as 2 hours or 30 minutes.")
    remaining = re.sub(r"\band\b", " ", text.strip(), flags=re.IGNORECASE)
    matches = list(_TOKEN.finditer(remaining))
    if not matches:
        raise ValueError("Tell me an amount and unit, such as 2 hours or 30 minutes.")

    consumed = "".join(match.group(0) for match in matches)
    normalize = lambda value: re.sub(r"[\s,]+", "", value).lower()
    if normalize(consumed) != normalize(remaining):
        raise ValueError("I couldn't understand that duration. Use weeks, days, hours, minutes, or seconds.")

    total = 0
    for match in matches:
        unit = match.group(2).lower()
        if unit not in _UNIT_SECONDS:
            raise ValueError(f"Unknown time unit: {unit}. Months are not supported; use days or weeks.")
        total += int(match.group(1)) * _UNIT_SECONDS[unit]
    if total <= 0:
        raise ValueError("Duration must be greater than zero.")
    return total
