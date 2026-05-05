"""Event linker — deduplicate extracted events using LLM disambiguation.

Pipeline (events only):

    new event → schema parse → geocode location → candidate filter
                                                  (event_type ∧
                                                   date overlap ∧
                                                   same level_2_id)
              → LLM disambiguation → match-id ? merge : create new

Themes and entities are not linked here — they were dropped from the
linker after the v1 evaluation (`src/PoC/event_linking.py` was the
original PoC; v1 of this module covered all three categories with
heuristic matching but produced poor recall on events).
"""

from __future__ import annotations

import copy
import logging
import random
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from src.schema.parse_object import Parser
from src.schema.schemas.read_schema import load_schema

from .geocode import geocode_location
from .link_llm import disambiguate

logger = logging.getLogger(__name__)


# Slack (in days, applied symmetrically) on the date window used for both
# candidate registration and lookup. Two events match in the date dimension
# whenever their slack-expanded windows share any day.
EXTRACTED_DATE_SLACK_DAYS = 1
PUBLICATION_DATE_SLACK_DAYS = 2


# ---------------------------------------------------------------------------
# Schema loading (mirrors the cache pattern in extract.py)
# ---------------------------------------------------------------------------

_SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "extraction" / "schemas"
_schema_cache: Dict[str, Dict[str, Any]] = {}


def _snake_to_pascal(name: str) -> str:
    return "".join(w.capitalize() for w in name.split("_"))


def _get_schema(supertype: str) -> Optional[Dict[str, Any]]:
    if supertype not in _schema_cache:
        path = _SCHEMAS_DIR / f"{supertype}.json"
        if not path.exists():
            return None
        _schema_cache[supertype] = load_schema(path)
    return _schema_cache[supertype]


def _category_for(supertype: str) -> str:
    loaded = _get_schema(supertype)
    if not loaded:
        return "event"
    schema_key = _snake_to_pascal(supertype)
    return loaded.get("meta", {}).get(schema_key, {}).get("category", "event")


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value or not isinstance(value, str):
        return None
    try:
        from dateutil.parser import parse as _parse

        return _parse(value)
    except Exception:
        return None


def _date_keys(
    start: Optional[datetime],
    end: Optional[datetime],
    slack_days: int = 0,
) -> List[str]:
    """Return all `YYYYMMDD` day keys spanning `[start - slack, end + slack]`."""
    if start is None and end is None:
        return []
    if start is None:
        start = end
    if end is None:
        end = start
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


def _resolve_window(
    record: Dict[str, Any],
) -> Tuple[Optional[datetime], Optional[datetime], int, str]:
    """Return (start, end, slack_days, source) for candidate-window lookup.

    Prefers the extracted ``date_range`` (with EXTRACTED_DATE_SLACK_DAYS).
    Falls back to ``date_created`` (the source article's publication
    timestamp, with PUBLICATION_DATE_SLACK_DAYS). Returns
    ``(None, None, 0, "missing")`` when neither is available.
    """
    dr = (record.get("date_range") or {}).get("date_range") or {}
    s = _parse_dt(dr.get("start"))
    e = _parse_dt(dr.get("end"))
    if s or e:
        return s, e, EXTRACTED_DATE_SLACK_DAYS, "extracted"

    pub = _parse_dt(record.get("date_created"))
    if pub:
        return pub, pub, PUBLICATION_DATE_SLACK_DAYS, "publication"

    return None, None, 0, "missing"


# ---------------------------------------------------------------------------
# Payload builder for the LLM disambiguator
# ---------------------------------------------------------------------------

_ADDRESS_FIELDS = (
    "country", "state", "city", "neighborhood",
    "zone", "street", "number", "place_name",
)


def _address(record: Dict[str, Any]) -> Dict[str, Any]:
    loc = record.get("location") or {}
    return {f: loc.get(f) for f in _ADDRESS_FIELDS}


def _date_payload(record: Dict[str, Any]) -> Dict[str, Optional[str]]:
    dr = (record.get("date_range") or {}).get("date_range") or {}
    s = _parse_dt(dr.get("start"))
    e = _parse_dt(dr.get("end"))
    return {
        "start": s.isoformat() if s else None,
        "end": e.isoformat() if e else None,
    }


def _llm_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    # Read whichever field carries the article publication timestamp:
    # `date_created` on raw extracted records, `publication_date` on linked records.
    pub = record.get("date_created") or record.get("publication_date")
    pub_dt = _parse_dt(pub)
    return {
        "name": record.get("name"),
        "description": record.get("description") or "",
        "address": _address(record),
        "date": _date_payload(record),
        "publication_date": pub_dt.isoformat() if pub_dt else None,
    }


# ---------------------------------------------------------------------------
# EntityLinker — events only
# ---------------------------------------------------------------------------


class EntityLinker:
    """Links extracted events into canonical event records via LLM disambiguation."""

    def __init__(self, geocode: bool = True):
        self.geocode = geocode

        # Linked events, keyed by minted id.
        self.events: Dict[str, Dict[str, Any]] = {}

        # Candidate index: (event_type, level_2_id, date_key) → set of event ids.
        self._event_index: Dict[Tuple[str, str, str], Set[str]] = defaultdict(set)

        # Drop counters for the run summary.
        self.dropped: Dict[str, int] = defaultdict(int)

    # -- Public API ----------------------------------------------------

    def link_all(self, records: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        records = list(records)
        logger.debug("Starting link_all over %d records", len(records))
        for raw in records:
            try:
                self._process(raw)
            except Exception as ex:
                logger.exception("Failed to link record (%s): %s", raw.get("_supertype"), ex)
                self.dropped["error"] += 1
        events = list(self.events.values())
        logger.debug(
            "link_all done — %d linked events, dropped=%s",
            len(events),
            dict(self.dropped),
        )
        for e in events:
            logger.debug(
                "  EVENT id=%s type=%s sources=%d name=%r desc=%r",
                e.get("id"),
                e.get("event_type"),
                len(e.get("source_ids") or []),
                e.get("name"),
                (e.get("description") or "")[:140],
            )
        return {"events": events}

    # -- Per-record processing ----------------------------------------

    def _process(self, raw: Dict[str, Any]) -> None:
        supertype = raw.get("_supertype")
        if not supertype:
            self.dropped["no_supertype"] += 1
            return

        category = _category_for(supertype)
        if category != "event":
            self.dropped[f"skipped_category:{category}"] += 1
            return

        meta = {
            "_source_id": raw.get("_source_id"),
            "_supertype": supertype,
            "date_created": raw.get("date_created"),
        }
        clean = {
            k: v
            for k, v in raw.items()
            if k not in ("_source_id", "_supertype", "date_created")
        }
        record = self._parse_with_schema(clean, supertype)
        record.update({k: v for k, v in meta.items() if v is not None})

        # Geocode the structured location (we now read level_2_id off the result).
        geo = geocode_location(record.get("location")) if self.geocode else None
        if geo:
            record["_geo"] = geo

        self._link_event(record)

    def _parse_with_schema(self, raw: Dict[str, Any], supertype: str) -> Dict[str, Any]:
        loaded = _get_schema(supertype)
        if not loaded:
            return copy.deepcopy(raw)
        schema_key = _snake_to_pascal(supertype)
        parser = Parser(loaded["schemas"])
        try:
            return parser.normalize_record(raw, schema_key, raise_validation_error=False)
        except Exception as ex:
            logger.warning("Schema parse failed for %s: %s", supertype, ex)
            return copy.deepcopy(raw)

    # -- Event linking -------------------------------------------------

    def _link_event(self, record: Dict[str, Any]) -> None:
        event_type = record.get("event_type")
        if not event_type:
            self.dropped["event_no_type"] += 1
            return

        start_dt, end_dt, slack_days, date_source = _resolve_window(record)
        if date_source == "missing":
            self.dropped["event_no_date_no_pub"] += 1
            return

        level_2_id = ((record.get("_geo") or {}).get("level_2_id") or "")
        # Candidate filter: event_type ∧ same level_2_id ∧ slack-expanded date overlap.
        date_keys = _date_keys(start_dt, end_dt, slack_days)
        candidate_ids: Set[str] = set()
        for dk in date_keys:
            candidate_ids |= self._event_index.get((event_type, level_2_id, dk), set())

        candidate_records = [
            {
                "id": cid,
                **_llm_payload(self.events[cid]),
            }
            for cid in candidate_ids
        ]

        match_id = disambiguate(_llm_payload(record), candidate_records)
        if match_id and match_id in self.events:
            base = self.events[match_id]
            logger.debug(
                "MERGE — incoming source=%s date_source=%s name=%r desc=%r\n"
                "        into id=%s name=%r existing_sources=%d desc=%r",
                record.get("_source_id"),
                date_source,
                record.get("name"),
                (record.get("description") or "")[:140],
                match_id,
                base.get("name"),
                len(base.get("source_ids") or []),
                (base.get("description") or "")[:140],
            )
            self._merge_event(base, record, start_dt, end_dt, slack_days, date_source)
        else:
            logger.debug(
                "CREATE — event_type=%s level_2_id=%r date_source=%s "
                "dates=%s..%s slack=%dd name=%r desc=%r (no match among %d candidates)",
                event_type,
                level_2_id,
                date_source,
                start_dt.isoformat() if start_dt else None,
                end_dt.isoformat() if end_dt else None,
                slack_days,
                record.get("name"),
                (record.get("description") or "")[:140],
                len(candidate_records),
            )
            self._create_event(record, start_dt, end_dt, slack_days, level_2_id, date_source)

    def _create_event(
        self,
        record: Dict[str, Any],
        start_dt: Optional[datetime],
        end_dt: Optional[datetime],
        slack_days: int,
        level_2_id: str,
        date_source: str,
    ) -> None:
        ref_dt = start_dt or end_dt
        slug = level_2_id or "noloc"
        eid = f"{ref_dt.strftime('%Y%m%d')}_{slug}_{random.randint(100000, 999999)}"
        linked = copy.deepcopy(record)
        linked["id"] = eid
        linked["source_ids"] = (
            [record["_source_id"]] if record.get("_source_id") else []
        )
        # Promote the article publication timestamp onto the canonical record
        # under its public name.
        pub = record.get("date_created") or record.get("publication_date")
        if pub:
            linked["publication_date"] = pub
        linked.pop("_source_id", None)
        linked.pop("date_created", None)
        # Track the earliest date_source on the linked record so future merges
        # know whether the canonical window came from extracted or publication
        # dates. Used purely for indexing — `_link_event` always re-resolves
        # the window for the incoming record.
        linked["_date_source"] = date_source
        self.events[eid] = linked

        # Register under both extracted-date and publication-date windows so
        # future incoming events find this one regardless of which source
        # they use.
        event_type = linked.get("event_type") or ""
        if date_source == "extracted":
            for dk in _date_keys(start_dt, end_dt, EXTRACTED_DATE_SLACK_DAYS):
                self._event_index[(event_type, level_2_id, dk)].add(eid)
        pub_dt = _parse_dt(linked.get("publication_date"))
        if pub_dt:
            for dk in _date_keys(pub_dt, pub_dt, PUBLICATION_DATE_SLACK_DAYS):
                self._event_index[(event_type, level_2_id, dk)].add(eid)

    def _merge_event(
        self,
        base: Dict[str, Any],
        new: Dict[str, Any],
        new_start: Optional[datetime],
        new_end: Optional[datetime],
        new_slack_days: int,
        new_date_source: str,
    ) -> None:
        # Append source id (de-duped).
        sid = new.get("_source_id")
        if sid and sid not in base["source_ids"]:
            base["source_ids"].append(sid)

        # Fillna for selected fields, including publication_date (keep the
        # earliest publication date we've seen).
        for f in ("name", "description", "context", "status"):
            if base.get(f) in (None, "", []) and new.get(f) not in (None, "", []):
                base[f] = new[f]
        new_pub = new.get("date_created") or new.get("publication_date")
        if new_pub:
            base_pub = base.get("publication_date")
            if not base_pub:
                base["publication_date"] = new_pub
            else:
                base_pub_dt = _parse_dt(base_pub)
                new_pub_dt = _parse_dt(new_pub)
                if base_pub_dt and new_pub_dt and new_pub_dt < base_pub_dt:
                    base["publication_date"] = new_pub

        # Widen the extracted date range when present on either side.
        base_dr = base.setdefault("date_range", {}).setdefault("date_range", {})
        base_s = _parse_dt(base_dr.get("start"))
        base_e = _parse_dt(base_dr.get("end"))
        new_extracted = new_date_source == "extracted"
        if new_extracted or base_s or base_e:
            ws = new_start if new_extracted else None
            we = new_end if new_extracted else None
            merged_s = base_s if base_s and (not ws or base_s <= ws) else ws
            merged_e = base_e if base_e and (not we or base_e >= we) else we
            base_dr["start"] = merged_s
            base_dr["end"] = merged_e

        # Promote location when the new record has more populated subfields and
        # belongs to the same state (level_2_id match).
        new_geo = new.get("_geo") or {}
        base_geo = base.get("_geo") or {}
        if new_geo.get("level_2_id") and new_geo.get("level_2_id") == base_geo.get("level_2_id"):
            new_loc = new.get("location") or {}
            base_loc = base.get("location") or {}
            if _populated_count(new_loc) > _populated_count(base_loc):
                base["location"] = new_loc
                base["_geo"] = new_geo

        # Re-register under any new day-keys the merge introduced. We reindex
        # under both the extracted-date window (with extracted slack) and the
        # publication-date window (with publication slack) so candidate lookup
        # finds this event no matter which date source the next incoming
        # record uses.
        event_type = base.get("event_type") or ""
        level_2_id = ((base.get("_geo") or {}).get("level_2_id") or "")
        merged_s = _parse_dt(base_dr.get("start"))
        merged_e = _parse_dt(base_dr.get("end"))
        if merged_s or merged_e:
            for dk in _date_keys(merged_s, merged_e, EXTRACTED_DATE_SLACK_DAYS):
                self._event_index[(event_type, level_2_id, dk)].add(base["id"])
        base_pub = _parse_dt(base.get("publication_date"))
        if base_pub:
            for dk in _date_keys(base_pub, base_pub, PUBLICATION_DATE_SLACK_DAYS):
                self._event_index[(event_type, level_2_id, dk)].add(base["id"])


def _populated_count(loc: Dict[str, Any]) -> int:
    if not isinstance(loc, dict):
        return 0
    return sum(1 for f in _ADDRESS_FIELDS if loc.get(f))
