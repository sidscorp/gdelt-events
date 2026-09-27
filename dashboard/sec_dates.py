"""Human-readable SEC date context with deterministic relative wording."""
from __future__ import annotations

from datetime import date, datetime, timezone


def _parse(value: str | date | datetime | None) -> date | datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value
    value = str(value).strip()
    if len(value) == 8 and value.isdigit():
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if "T" in value else date.fromisoformat(value)
    except ValueError:
        return None


def format_date_context(value: str | date | datetime | None, *, now: datetime | None = None,
                        label: str | None = None) -> str:
    """Return an absolute date plus stable relative context, or ``not stated``.

    ``now`` is injectable so page rendering and tests do not depend on a clock.
    A timestamp is shown in its supplied timezone; date-only SEC fields remain dates.
    """
    parsed = _parse(value)
    prefix = f"{label}: " if label else ""
    if parsed is None:
        return prefix + "not stated"
    now = now or datetime.now(timezone.utc)
    zone = parsed.tzinfo if isinstance(parsed, datetime) and parsed.tzinfo else timezone.utc
    today = now.astimezone(zone).date()
    value_date = parsed.date() if isinstance(parsed, datetime) else parsed
    days = (today - value_date).days
    relative = "today" if days == 0 else (f"{days} day{'s' if days != 1 else ''} ago" if days > 0 else f"in {-days} day{'s' if days != -1 else ''}")
    # ``%-d`` is unavailable on Windows; composing the day is portable.
    absolute = f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"
    if isinstance(parsed, datetime):
        zone_name = parsed.tzname() or parsed.strftime("UTC%z")
        hour = parsed.strftime("%I").lstrip("0") or "0"
        absolute += f", {hour}:{parsed.strftime('%M %p')} " + zone_name
    return f"{prefix}{absolute} · {relative}"
