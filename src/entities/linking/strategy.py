"""Per-supertype linking strategies — geo-event strategy v1.

A strategy owns the supertype-specific lifecycle of identification:

    enrich → date-window / partition-key construction → adjudication
           → merge-or-create → (re)index

`EntityLinker` (link.py) stays supertype-agnostic: it parses the record
envelope, selects a strategy by schema category, and orchestrates these
calls against a `CandidateIndex` (index.py).

`GeoEventStrategy` implements the geo-event identity model: two records
denote the same event iff same class (`event_type`), same place
(state-level partition; address-level left to the LLM), overlapping
time (tiered fallbacks: extracted date → publication date), and
co-referent content (LLM adjudication over name/description).

Every precision fix is a constructor parameter so the legacy behaviour
can be reproduced exactly for regression runs (see the
behaviour-preserving values in each parameter's docstring line).
"""

from __future__ import annotations

import copy
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from .geocode import geocode_location
from .index import CandidateIndex, IndexKey
from .link_llm import disambiguate
from .mx_states import normalize_state, slug

logger = logging.getLogger(__name__)


# Default slack (in days, applied symmetrically) on the date window used
# for both candidate registration and lookup. Two events match in the
# date dimension whenever their slack-expanded windows share any day.
EXTRACTED_DATE_SLACK_DAYS = 1
PUBLICATION_DATE_SLACK_DAYS = 2


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


@dataclass
class DateWindow:
    """Resolved candidate window for one record.

    `source` tracks provenance: "extracted" (the article states when the
    event happened), "publication" (fallback to the article timestamp),
    or "missing" (neither — the record is dropped).
    """

    start: Optional[datetime]
    end: Optional[datetime]
    slack_days: int
    source: str
    precision_days: Optional[int] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "slack_days": self.slack_days,
            "source": self.source,
            "precision_days": self.precision_days,
        }


@dataclass
class PreparedEvent:
    """Enriched record plus the resolved keys the linker needs."""

    record: Dict[str, Any]
    event_type: str
    geo_key: str
    window: DateWindow


# ---------------------------------------------------------------------------
# Payload builder for the LLM disambiguator
# (moved verbatim from link.py — the sha256 cache keys depend on this shape)
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


def _populated_count(loc: Dict[str, Any]) -> int:
    if not isinstance(loc, dict):
        return 0
    return sum(1 for f in _ADDRESS_FIELDS if loc.get(f))


# ---------------------------------------------------------------------------
# GeoEventStrategy
# ---------------------------------------------------------------------------


class GeoEventStrategy:
    """Identification strategy for events whose identity is class + place + time."""

    def __init__(
        self,
        geocode: bool = True,
        extracted_slack_days: int = EXTRACTED_DATE_SLACK_DAYS,
        publication_slack_days: int = PUBLICATION_DATE_SLACK_DAYS,
        geo_partition_field: str = "level_2",          # legacy: "level_2_id" (never set by geocode.py → "" bucket)
        state_catalogue_fallback: bool = True,          # legacy: False
        precision_aware_slack: bool = True,             # legacy: False (fixed ±1)
        max_window_days: int = 365,
        clamp_long_ranges: bool = True,                 # legacy: False (endpoints-only quirk)
        bounded_merge_widening: bool = True,            # legacy: False (unconditional min/max)
        candidate_cap: Optional[int] = 12,              # legacy: None (unbounded)
        probe_noloc_bucket: bool = True,                # located lookups also probe the "" bucket
    ):
        self.geocode = geocode
        self.extracted_slack_days = extracted_slack_days
        self.publication_slack_days = publication_slack_days
        self.geo_partition_field = geo_partition_field
        self.state_catalogue_fallback = state_catalogue_fallback
        self.precision_aware_slack = precision_aware_slack
        self.max_window_days = max_window_days
        self.clamp_long_ranges = clamp_long_ranges
        self.bounded_merge_widening = bounded_merge_widening
        self.candidate_cap = candidate_cap
        self.probe_noloc_bucket = probe_noloc_bucket

    # -- Prepare: enrich + resolve identity keys ------------------------

    def prepare(
        self, record: Dict[str, Any]
    ) -> Tuple[Optional[PreparedEvent], Optional[str]]:
        """Enrich the record and resolve its identity keys.

        Returns `(prepared, None)` or `(None, drop_reason)`.
        """
        if self.geocode:
            geo = geocode_location(record.get("location"))
            if geo:
                record["_geo"] = geo

        event_type = record.get("event_type")
        if not event_type:
            return None, "event_no_type"

        window = self._resolve_window(record)
        if window.source == "missing":
            return None, "event_no_date_no_pub"

        geo_key = self._geo_key(record)
        return PreparedEvent(record, event_type, geo_key, window), None

    def _geo_key(self, record: Dict[str, Any]) -> str:
        """Partition key for the place dimension, with tiered fallbacks.

        Tier 1: geocoder `level_2`, normalized through the state catalogue.
        Tier 2: extracted `location.state` text matched against the catalogue.
        Tier 3: "" — the explicit "noloc" bucket.
        Provenance is recorded on the record as `_geo_source`.
        """
        geo = record.get("_geo") or {}
        if self.geo_partition_field == "level_2_id":
            # Legacy mode: geocode.py never emits this key, so the
            # partition degrades to the "" bucket — kept only for
            # behaviour-preserving regression runs.
            record["_geo_source"] = "geocoder" if geo else "none"
            return geo.get("level_2_id") or ""

        level_2 = (geo.get("level_2") or "").strip()
        if level_2:
            record["_geo_source"] = "geocoder"
            return normalize_state(level_2) or slug(level_2)

        if self.state_catalogue_fallback:
            state_text = (record.get("location") or {}).get("state")
            state_slug = normalize_state(state_text)
            if state_slug:
                record["_geo_source"] = "state_catalogue"
                return state_slug

        record["_geo_source"] = "none"
        return ""

    def _resolve_window(self, record: Dict[str, Any]) -> DateWindow:
        """Resolve the candidate window: extracted date → publication date → missing."""
        dr_block = record.get("date_range") or {}
        dr = dr_block.get("date_range") or {}
        s = _parse_dt(dr.get("start"))
        e = _parse_dt(dr.get("end"))
        if s or e:
            precision: Optional[int] = None
            try:
                raw = dr_block.get("precision_days")
                precision = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                precision = None
            slack = self.extracted_slack_days
            if self.precision_aware_slack and precision:
                slack = max(slack, min(precision, self.max_window_days))
            return DateWindow(s, e, slack, "extracted", precision)

        pub = _parse_dt(record.get("date_created"))
        if pub:
            return DateWindow(pub, pub, self.publication_slack_days, "publication")

        return DateWindow(None, None, 0, "missing")

    def _date_keys(
        self,
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
        if days > self.max_window_days:
            if not self.clamp_long_ranges:
                # Legacy quirk: only the endpoints get indexed, so
                # overlap detection misses every day in between.
                return [s.strftime("%Y%m%d"), e.strftime("%Y%m%d")]
            logger.warning(
                "Date window %s..%s spans %dd > %dd — clamping end",
                s.isoformat(), e.isoformat(), days, self.max_window_days,
            )
            e = s + timedelta(days=self.max_window_days)
            days = self.max_window_days
        return [(s + timedelta(days=i)).strftime("%Y%m%d") for i in range(days + 1)]

    # -- Retrieval keys --------------------------------------------------

    def lookup_keys(self, prep: PreparedEvent) -> List[IndexKey]:
        """Keys to probe for candidates.

        Located records additionally probe the "" (noloc) bucket so an
        event first seen without a location can still be matched by later,
        located mentions. The reverse direction is impossible — a noloc
        record can't know which geo partition to probe.
        """
        day_keys = self._date_keys(
            prep.window.start, prep.window.end, prep.window.slack_days
        )
        keys = [(prep.event_type, prep.geo_key, dk) for dk in day_keys]
        if self.probe_noloc_bucket and prep.geo_key:
            keys += [(prep.event_type, "", dk) for dk in day_keys]
        return keys

    def _register(
        self,
        linked: Dict[str, Any],
        event_type: str,
        geo_key: str,
        window: Optional[DateWindow],
        index: CandidateIndex,
    ) -> None:
        """Register a linked event under its extracted and publication windows."""
        if window is not None and window.source == "extracted":
            for dk in self._date_keys(window.start, window.end, window.slack_days):
                index.register((event_type, geo_key, dk), linked["id"])
        pub_dt = _parse_dt(linked.get("publication_date"))
        if pub_dt:
            for dk in self._date_keys(pub_dt, pub_dt, self.publication_slack_days):
                index.register((event_type, geo_key, dk), linked["id"])

    # -- Adjudication ----------------------------------------------------

    def adjudicate(
        self,
        prep: PreparedEvent,
        candidate_ids: Set[str],
        events: Dict[str, Dict[str, Any]],
    ) -> Optional[str]:
        """Decide whether the incoming event matches a candidate (LLM call)."""
        kept = candidate_ids
        if self.candidate_cap is not None and len(candidate_ids) > self.candidate_cap:
            ordered = sorted(
                candidate_ids,
                key=lambda cid: str(events[cid].get("publication_date") or ""),
                reverse=True,
            )
            kept = ordered[: self.candidate_cap]
            logger.info(
                "Candidate cap: %d → %d for event_type=%s geo_key=%r",
                len(candidate_ids), len(kept), prep.event_type, prep.geo_key,
            )
        candidate_records = [
            {
                "id": cid,
                **_llm_payload(events[cid]),
            }
            for cid in kept
        ]
        return disambiguate(_llm_payload(prep.record), candidate_records)

    # -- Create ----------------------------------------------------------

    def create(
        self, prep: PreparedEvent, index: CandidateIndex
    ) -> Tuple[str, Dict[str, Any]]:
        record, window = prep.record, prep.window
        ref_dt = window.start or window.end
        slug_part = (prep.geo_key or "noloc").replace(" ", "-")
        eid = f"{ref_dt.strftime('%Y%m%d')}_{slug_part}_{random.randint(100000, 999999)}"
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
        # Window provenance: `_date_source` mirrors the first window's source;
        # `_source_windows` accumulates every source's resolved window so the
        # canonical date range can be chosen (not just widened) on merges.
        linked["_date_source"] = window.source
        linked["_source_windows"] = [window.to_json()]
        self._register(linked, prep.event_type, prep.geo_key, window, index)
        return eid, linked

    # -- Merge -----------------------------------------------------------

    def merge(
        self,
        base: Dict[str, Any],
        prep: PreparedEvent,
        index: CandidateIndex,
    ) -> None:
        new, window = prep.record, prep.window

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

        base.setdefault("_source_windows", []).append(window.to_json())

        # Canonical date range policy.
        base_dr = base.setdefault("date_range", {}).setdefault("date_range", {})
        if self.bounded_merge_widening:
            self._apply_best_window(base, base_dr)
        else:
            # Legacy: unconditional min/max widening.
            base_s = _parse_dt(base_dr.get("start"))
            base_e = _parse_dt(base_dr.get("end"))
            new_extracted = window.source == "extracted"
            if new_extracted or base_s or base_e:
                ws = window.start if new_extracted else None
                we = window.end if new_extracted else None
                merged_s = base_s if base_s and (not ws or base_s <= ws) else ws
                merged_e = base_e if base_e and (not we or base_e >= we) else we
                base_dr["start"] = merged_s
                base_dr["end"] = merged_e

        # Promote location when the new record has more populated subfields
        # and resolves to the same geo partition.
        if self._same_geo_partition(base, new, prep.geo_key):
            new_loc = new.get("location") or {}
            base_loc = base.get("location") or {}
            if _populated_count(new_loc) > _populated_count(base_loc):
                base["location"] = new_loc
                base["_geo"] = new.get("_geo") or {}
                if new.get("_geo_source"):
                    base["_geo_source"] = new["_geo_source"]

        # Re-register so candidate lookup finds this event under any new
        # day-keys the merge introduced. Old keys stay registered (the index
        # is append-only), so recall never shrinks.
        base_geo_key = self._geo_key(base)
        if self.bounded_merge_widening:
            # Register the incoming record's window directly; the canonical
            # range no longer widens, so it can't be used for reindexing.
            self._register(base, prep.event_type, base_geo_key, window, index)
        else:
            merged_s = _parse_dt(base_dr.get("start"))
            merged_e = _parse_dt(base_dr.get("end"))
            if merged_s or merged_e:
                merged_window = DateWindow(
                    merged_s, merged_e, self.extracted_slack_days, "extracted"
                )
                self._register(base, prep.event_type, base_geo_key, merged_window, index)
            else:
                self._register(base, prep.event_type, base_geo_key, None, index)

    def _apply_best_window(
        self, base: Dict[str, Any], base_dr: Dict[str, Any]
    ) -> None:
        """Set the canonical date range to the most precise extracted window.

        Extracted beats publication; smaller `precision_days` wins
        (None counts as exact, i.e. 0); ties keep the earliest-seen window.
        """
        extracted = [
            w for w in base.get("_source_windows", []) if w.get("source") == "extracted"
        ]
        if not extracted:
            return
        best = min(extracted, key=lambda w: w.get("precision_days") or 0)
        base_dr["start"] = _parse_dt(best.get("start"))
        base_dr["end"] = _parse_dt(best.get("end"))
        base.setdefault("date_range", {})["precision_days"] = best.get("precision_days")

    def _same_geo_partition(
        self, base: Dict[str, Any], new: Dict[str, Any], new_geo_key: str
    ) -> bool:
        if self.geo_partition_field == "level_2_id":
            # Legacy comparison — geocode.py never emits level_2_id, so this
            # is always False (location promotion never fired).
            new_geo = new.get("_geo") or {}
            base_geo = base.get("_geo") or {}
            return bool(
                new_geo.get("level_2_id")
                and new_geo.get("level_2_id") == base_geo.get("level_2_id")
            )
        return bool(new_geo_key) and new_geo_key == self._geo_key(base)


# ---------------------------------------------------------------------------
# Registry — category → strategy
# ---------------------------------------------------------------------------


def build_strategies(
    geocode: bool = True,
    strategy_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the category→strategy registry used by `EntityLinker`.

    Categories without an entry ("theme", "entity") are declared skips —
    the linker tallies them and moves on. A supertype with no schema at
    all is a logged drop, handled before strategy selection.
    """
    params = dict(strategy_params or {})
    return {"event": GeoEventStrategy(geocode=geocode, **params)}
