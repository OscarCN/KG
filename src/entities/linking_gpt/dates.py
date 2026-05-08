"""Date-window helpers shared by linking_gpt event linking."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

EXTRACTED_DATE_SLACK_DAYS = 1
PUBLICATION_DATE_SLACK_DAYS = 2


def parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value or not isinstance(value, str):
        return None
    try:
        from dateutil.parser import parse

        return parse(value)
    except Exception:
        return None


def date_keys(
    start: Optional[datetime],
    end: Optional[datetime],
    slack_days: int = 0,
) -> list[str]:
    if start is None and end is None:
        return []
    if start is None:
        start = end
    if end is None:
        end = start
    assert start is not None and end is not None
    s = start.date()
    e = end.date()
    if s > e:
        s, e = e, s
    s = s - timedelta(days=slack_days)
    e = e + timedelta(days=slack_days)
    days = (e - s).days
    if days > 365:
        return [s.strftime("%Y%m%d"), e.strftime("%Y%m%d")]
    return [(s + timedelta(days=i)).strftime("%Y%m%d") for i in range(days + 1)]


def resolve_event_window(record: dict[str, Any]) -> tuple[Optional[datetime], Optional[datetime], int, str]:
    dr = (record.get("date_range") or {}).get("date_range") or {}
    start = parse_dt(dr.get("start"))
    end = parse_dt(dr.get("end"))
    if start or end:
        return start, end, EXTRACTED_DATE_SLACK_DAYS, "extracted"

    publication = parse_dt(record.get("date_created") or record.get("publication_date"))
    if publication:
        return publication, publication, PUBLICATION_DATE_SLACK_DAYS, "publication"

    return None, None, 0, "missing"
