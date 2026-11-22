"""Date-range resolution shared by the backfill scripts.

GDELT file timestamps (``YYYYMMDDHHMMSS`` in masterfilelist.txt and in GAL file
names) are UTC. Every bound returned here is therefore a *naive datetime in UTC*,
so callers can format it with ``strftime`` and compare it directly against those
timestamps. Using local time instead silently shifts the range by the machine's
UTC offset (e.g. 5 hours in EST), which is the bug fixed in issue #46.
"""

from datetime import datetime, timedelta, timezone

DATE_FMT = "%Y-%m-%d"


def utc_now() -> datetime:
    """Current time as a naive UTC datetime. Tests patch this to pin the clock."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def resolve_range(
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
) -> tuple[datetime, datetime]:
    """Return ``(since, until)`` as naive UTC datetimes.

    - ``since`` / ``until`` are ``YYYY-MM-DD`` dates interpreted as UTC midnight.
    - Without ``since``, the start is ``days`` before now (UTC).
    - Without ``until``, the end is now (UTC), so the newest published slot is included.
    """
    now = utc_now()
    if since:
        start = datetime.strptime(since, DATE_FMT)
    elif days is not None:
        start = now - timedelta(days=days)
    else:
        raise ValueError("either days or since is required")
    end = datetime.strptime(until, DATE_FMT) if until else now
    return start, end
