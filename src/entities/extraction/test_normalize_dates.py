"""Tests for the extraction-side date timezone normalization
(`_normalize_date_timezones` in extract.py): UTC-stamped and naive datetimes
are re-anchored (wall clock kept) to the record's extracted `timezone` when
valid, else America/Mexico_City; genuine non-zero offsets are kept."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.entities.extraction.extract import _normalize_date_timezones

_MX = ZoneInfo("America/Mexico_City")


def _record(start, end=None, tz=None):
    return {
        "name": "ev",
        "date_range": {
            "date_range": {"start": start, "end": end},
            "timezone": tz,
            "mention": "10:00 horas",
            "precision_days": 0,
        },
    }


def test_utc_stamp_reanchored_to_mexico_city():
    # "10:00 horas" stamped +00:00 by a UTC-clocked container — wall clock kept
    rec = _record(datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc))
    _normalize_date_timezones(rec)
    start = rec["date_range"]["date_range"]["start"]
    assert start.hour == 10  # wall clock preserved
    assert start.utcoffset() == timedelta(hours=-6)
    assert rec["date_range"]["timezone"] == "America/Mexico_City"


def test_naive_datetime_anchored_to_default():
    rec = _record(datetime(2026, 8, 6, 10, 0))
    _normalize_date_timezones(rec)
    assert rec["date_range"]["date_range"]["start"].tzinfo == _MX


def test_extracted_timezone_field_honored():
    rec = _record(datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc),
                  tz="America/Cancun")
    _normalize_date_timezones(rec)
    start = rec["date_range"]["date_range"]["start"]
    assert start.utcoffset() == timedelta(hours=-5)  # Cancún, no DST
    assert rec["date_range"]["timezone"] == "America/Cancun"


def test_invalid_timezone_field_falls_back_to_default():
    rec = _record(datetime(2026, 8, 6, 10, 0), tz="hora local")
    _normalize_date_timezones(rec)
    assert rec["date_range"]["date_range"]["start"].tzinfo == _MX
    assert rec["date_range"]["timezone"] == "America/Mexico_City"


def test_nonzero_offset_kept_verbatim():
    stamped = datetime(2026, 8, 6, 10, 0, tzinfo=timezone(timedelta(hours=-6)))
    rec = _record(stamped)
    _normalize_date_timezones(rec)
    assert rec["date_range"]["date_range"]["start"] is stamped


def test_nulls_and_non_dates_untouched():
    rec = _record(None)
    rec["tags"] = ["a", "b"]
    _normalize_date_timezones(rec)
    assert rec["date_range"]["date_range"]["start"] is None
    assert rec["tags"] == ["a", "b"]


def test_walks_nested_lists_and_composites():
    # e.g. a List[DateRangeFromUnstructured] field, or date composites nested
    # anywhere the schema puts them
    rec = {"dates": [_record(datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc))["date_range"]],
           "completion": {"date": datetime(2026, 9, 1, 0, 0),
                          "mention": "septiembre", "precision_days": 30}}
    _normalize_date_timezones(rec)
    assert rec["dates"][0]["date_range"]["start"].tzinfo == _MX
    assert rec["completion"]["date"].tzinfo == _MX
