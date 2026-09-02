"""Date and time helpers.

Everything the HRM domain reasons about — shifts, leave spans, payroll
periods — is a *local business* concept anchored to a company timezone, while
everything we store is UTC and timezone-aware. These helpers are the only
place that conversion happens.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MINUTES_PER_DAY = 24 * 60


def utcnow() -> datetime:
    """Timezone-aware current time. Never use `datetime.utcnow()`."""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Coerce a datetime to UTC, treating a naive value as already UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Look up a company/location timezone, falling back to UTC.

    An unknown zone must not take down attendance capture, so this degrades
    rather than raising.
    """
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_date(value: datetime, timezone_name: str | None) -> date:
    """The calendar date a UTC instant falls on in a company's timezone."""
    return as_utc(value).astimezone(resolve_timezone(timezone_name)).date()


def business_today(timezone_name: str | None = None) -> date:
    """Today, as the *company* reckons it.

    Never `date.today()`, which answers in the server's local timezone. A
    server in UTC would tell a Manila office it is still yesterday for eight
    hours every day, and "did this leave start yet?" would be wrong for all of
    them.
    """
    return local_date(utcnow(), timezone_name)


def date_range(start: date, end: date) -> Iterator[date]:
    """Every date from `start` to `end`, inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def days_inclusive(start: date, end: date) -> int:
    """Calendar days spanned, counting both endpoints. Negative spans give 0."""
    if end < start:
        return 0
    return (end - start).days + 1


def is_weekend(value: date, weekend_days: frozenset[int] = frozenset({5, 6})) -> bool:
    """True when `value` falls on a weekend.

    `weekend_days` uses `date.weekday()` numbering (Monday = 0), and defaults
    to Saturday/Sunday. It is a parameter because the working week is not
    Saturday/Sunday everywhere — several markets rest on Friday/Saturday.
    """
    return value.weekday() in weekend_days


def minutes_between(start: time, end: time) -> int:
    """Minutes from `start` to `end`, wrapping past midnight.

    An overnight shift (22:00 → 06:00) is expressed as `end < start` and
    yields 480 minutes rather than a negative span.
    """
    start_minutes = start.hour * 60 + start.minute
    end_minutes = end.hour * 60 + end.minute
    if end_minutes <= start_minutes:
        end_minutes += MINUTES_PER_DAY
    return end_minutes - start_minutes


def spans_midnight(start: time, end: time) -> bool:
    """True when a shift's end time falls on the following calendar day."""
    return (end.hour * 60 + end.minute) <= (start.hour * 60 + start.minute)


def combine_shift_datetime(
    day: date, moment: time, *, next_day: bool = False, timezone_name: str | None = None
) -> datetime:
    """Anchor a shift time to a calendar day in the company timezone, as UTC."""
    anchor = day + timedelta(days=1) if next_day else day
    local = datetime.combine(anchor, moment, tzinfo=resolve_timezone(timezone_name))
    return local.astimezone(UTC)


def overlaps(start_a: date, end_a: date | None, start_b: date, end_b: date | None) -> bool:
    """Do two closed date ranges overlap? `None` means open-ended."""
    if end_a is not None and end_a < start_b:
        return False
    return not (end_b is not None and end_b < start_a)


def year_of(value: date) -> int:
    return value.year
