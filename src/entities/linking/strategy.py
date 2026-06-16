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
from .geo_util import grid_cell, grid_neighbors, haversine
from .index import CandidateIndex, IndexKey
from .link_llm import disambiguate
from .mx_states import normalize_state, slug
from .text_util import name_similarity

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
    partition: str = ""  # type dimension of the candidate partition (supertype or event_type)


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


def _identification_text(record: Dict[str, Any]) -> str:
    """Description-led identification block, with the name folded in.

    Many events have no name, so the LLM should judge on the *described facts*;
    the name is included only as an extra clue when present (and not already in
    the description), never as a privileged key.
    """
    desc = (record.get("description") or "").strip()
    name = (record.get("name") or "").strip()
    if name and name.lower() not in desc.lower():
        return f"{desc} (nombre: {name})" if desc else name
    return desc


def _llm_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    # Read whichever field carries the article publication timestamp:
    # `date_created` on raw extracted records, `publication_date` on linked records.
    pub = record.get("date_created") or record.get("publication_date")
    pub_dt = _parse_dt(pub)
    return {
        "identification": _identification_text(record),
        "event_type": record.get("event_type"),
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
        geo_retrieval: str = "hierarchy",               # legacy: "level_2" (single state slug)
        partition_on: str = "supertype",                # type dimension of the candidate partition: "supertype" (soft type — retrieve across leaf event_types) or "event_type" (legacy hard type)
        partition_levels: Tuple[int, ...] = (3, 5, 6, 7),  # admin levels (below state) to bucket on
        grid_size_deg: float = 0.01,                    # coordinate grid cell side (~1.1 km)
        deterministic_merge: bool = True,               # skip the LLM on high-confidence matches
        deterministic_share_levels: Tuple[int, ...] = (6, 7),  # share one of these level_N_ids ⇒ same place
        det_day_slack: int = 1,                          # date-overlap slack for the deterministic gate
        det_publication_levels: Tuple[int, ...] = (7,),  # share level ⇒ publication-date overlap is enough (no extracted date needed)
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
        self.geo_retrieval = geo_retrieval
        self.partition_on = partition_on
        self.partition_levels = partition_levels
        self.grid_size_deg = grid_size_deg
        self.deterministic_merge = deterministic_merge
        self.deterministic_share_levels = deterministic_share_levels
        self.det_day_slack = det_day_slack
        self.det_publication_levels = det_publication_levels

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
        # Type dimension of the candidate partition. With "supertype" (default),
        # candidates span all leaf event_types of the supertype (soft type) and
        # the leaf-type judgment is left to the deterministic gate / LLM. "event_type"
        # reproduces the legacy hard-type partition.
        partition = (
            record.get("_supertype") if self.partition_on == "supertype" else None
        ) or event_type
        return PreparedEvent(record, event_type, geo_key, window, partition=partition), None

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

    def _fine_geo_keys(self, geo: Dict[str, Any]) -> List[str]:
        """Namespaced partition keys below state: level_N_id buckets + grid cell."""
        keys: List[str] = []
        for n in self.partition_levels:
            lid = (geo.get(f"level_{n}_id") or "").strip()
            if lid:
                keys.append(f"l{n}:{lid}")
        return keys

    def _located_geo_keys(self, geo: Dict[str, Any]) -> List[str]:
        """Fine keys for a geocoded record: level_N_id buckets + its grid cell.

        Empty when the record has no admin ids and no coordinates (not located).
        """
        keys = self._fine_geo_keys(geo)
        cell = grid_cell(geo.get("matched_lat"), geo.get("matched_lon"), self.grid_size_deg)
        if cell is not None:
            keys.append(f"g:{cell[0]},{cell[1]}")
        return keys

    def _register_geo_keys(self, record: Dict[str, Any]) -> List[str]:
        """Geo keys a record is registered under.

        Hierarchy mode: a **located** record registers under its fine keys only
        (`level_N_id` buckets + grid cell) — deliberately *not* a shared
        state-wide bucket, which would re-merge every located event in the state.
        A record with no fine keys falls back to a state-only bucket (`so:<slug>`)
        or, with no state at all, the noloc bucket (`""`).
        """
        if self.geo_retrieval == "level_2":
            return [self._geo_key(record)]
        keys = self._located_geo_keys(record.get("_geo") or {})
        if keys:
            return list(dict.fromkeys(keys))
        state = self._geo_key(record)
        return [f"so:{state}"] if state else [""]

    def _lookup_geo_keys(self, record: Dict[str, Any]) -> List[str]:
        """Geo keys a record probes for candidates.

        A located record probes its fine keys + the grid cell **and its 8
        neighbors** (a same-event mention can land in an adjacent cell), plus —
        as a *bridge* — the state-only bucket and the noloc bucket, so a precise
        mention can still meet an earlier vague one. It does **not** probe a
        shared state-wide bucket, keeping the candidate set narrow. The reverse
        bridge (vague → precise) is impossible, as before.
        """
        if self.geo_retrieval == "level_2":
            gk = self._geo_key(record)
            keys = [gk]
            if self.probe_noloc_bucket and gk:
                keys.append("")
            return keys
        geo = record.get("_geo") or {}
        fine = self._fine_geo_keys(geo)
        cell = grid_cell(geo.get("matched_lat"), geo.get("matched_lon"), self.grid_size_deg)
        state = self._geo_key(record)
        if fine or cell is not None:
            keys = list(fine)
            if cell is not None:
                keys += [f"g:{r},{c}" for r, c in grid_neighbors(cell)]
            if self.probe_noloc_bucket:
                if state:
                    keys.append(f"so:{state}")
                keys.append("")
            return list(dict.fromkeys(keys))
        # Not located: probe the state-only bucket (+ noloc bridge).
        keys = [f"so:{state}"] if state else [""]
        if self.probe_noloc_bucket and state:
            keys.append("")
        return list(dict.fromkeys(keys))

    def lookup_keys(self, prep: PreparedEvent) -> List[IndexKey]:
        """Candidate-probe keys: every geo key crossed with every day key.

        The type dimension is `prep.partition` (the supertype by default), so
        retrieval spans all leaf event_types of the supertype — the leaf-type
        judgment is left to the deterministic gate / LLM (soft type).
        """
        day_keys = self._date_keys(
            prep.window.start, prep.window.end, prep.window.slack_days
        )
        geo_keys = self._lookup_geo_keys(prep.record)
        return [(prep.partition, gk, dk) for gk in geo_keys for dk in day_keys]

    def _register(
        self,
        linked: Dict[str, Any],
        partition: str,
        window: Optional[DateWindow],
        index: CandidateIndex,
    ) -> None:
        """Register a linked event under all its geo keys × its date windows.

        `partition` is the type dimension of the index key (supertype by default).
        """
        geo_keys = self._register_geo_keys(linked)
        if window is not None and window.source == "extracted":
            for dk in self._date_keys(window.start, window.end, window.slack_days):
                for gk in geo_keys:
                    index.register((partition, gk, dk), linked["id"])
        pub_dt = _parse_dt(linked.get("publication_date"))
        if pub_dt:
            for dk in self._date_keys(pub_dt, pub_dt, self.publication_slack_days):
                for gk in geo_keys:
                    index.register((partition, gk, dk), linked["id"])

    # -- Adjudication ----------------------------------------------------

    def _fine_id_set(self, geo: Dict[str, Any]) -> Set[str]:
        """Namespaced level ids the deterministic gate keys on (levels 6/7).

        Two events 'share a street/place' iff these sets intersect. A coarse
        record (no `level_6/7_id`) yields an empty set, so it can never match
        deterministically — which is what makes the gate precision-aware.
        """
        ids: Set[str] = set()
        for n in self.deterministic_share_levels:
            lid = (geo.get(f"level_{n}_id") or "").strip()
            if lid:
                ids.add(f"l{n}:{lid}")
        return ids

    @staticmethod
    def _cand_window(
        cand: Dict[str, Any]
    ) -> Tuple[Optional[datetime], Optional[datetime], Optional[int]]:
        """The candidate's canonical extracted window (start, end, precision_days)."""
        dr_block = cand.get("date_range") or {}
        dr = dr_block.get("date_range") or {}
        return _parse_dt(dr.get("start")), _parse_dt(dr.get("end")), dr_block.get("precision_days")

    def _cand_date_window(
        self, cand: Dict[str, Any]
    ) -> Optional[Tuple[Optional[datetime], Optional[datetime], bool]]:
        """Candidate's best date window for the gate: (start, end, is_publication).

        Prefers the extracted canonical range; falls back to the candidate's
        publication timestamp. None when the candidate has no date at all.
        """
        cs, ce, _ = self._cand_window(cand)
        if cs is not None or ce is not None:
            return cs, ce, False
        pub = _parse_dt(cand.get("publication_date"))
        if pub is not None:
            return pub, pub, True
        return None

    def _leaf_relation(self, ga: Dict[str, Any], gb: Dict[str, Any]) -> Optional[str]:
        """'misma' / 'distinta' / None — do two records resolve to the same fine
        (street/place) location? None when either lacks any fine id to compare."""
        a, b = self._fine_id_set(ga), self._fine_id_set(gb)
        if not a or not b:
            return None
        return "misma" if (a & b) else "distinta"

    @staticmethod
    def _geo_distance_m(ga: Dict[str, Any], gb: Dict[str, Any]) -> Optional[float]:
        """Haversine meters between two geo blocks, or None if either lacks coords."""
        la, lo = ga.get("matched_lat"), ga.get("matched_lon")
        lb, ob = gb.get("matched_lat"), gb.get("matched_lon")
        if None in (la, lo, lb, ob):
            return None
        return haversine(la, lo, lb, ob)

    @staticmethod
    def _date_overlap(
        s1: Optional[datetime], e1: Optional[datetime],
        s2: Optional[datetime], e2: Optional[datetime], slack_days: int,
    ) -> bool:
        a_s, a_e = (s1 or e1), (e1 or s1)
        b_s, b_e = (s2 or e2), (e2 or s2)
        if a_s is None or b_s is None:
            return False
        a_s = a_s - timedelta(days=slack_days)
        a_e = a_e + timedelta(days=slack_days)
        return a_s.date() <= b_e.date() and b_s.date() <= a_e.date()

    def _candidate_debug(
        self, prep: PreparedEvent, candidate_ids: Set[str], events: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Per-candidate {id, name, geo_dist_m, name_sim} for the case log."""
        inc_geo = prep.record.get("_geo") or {}
        inc_name = prep.record.get("name")
        out: List[Dict[str, Any]] = []
        for cid in candidate_ids:
            cand = events.get(cid) or {}
            d = self._geo_distance_m(inc_geo, cand.get("_geo") or {})
            both_named = bool(inc_name) and bool(cand.get("name"))
            out.append({
                "id": cid,
                "name": cand.get("name"),
                "geo_dist_m": round(d, 1) if d is not None else None,
                "name_sim": round(name_similarity(inc_name, cand.get("name")), 3) if both_named else None,
            })
        return out

    def _deterministic_match(
        self, prep: PreparedEvent, candidate_ids: Set[str], events: Dict[str, Dict[str, Any]],
    ) -> Optional[str]:
        """First candidate that shares a fine location id with the incoming
        event (same street/place) on an overlapping day, else None.

        Name-agnostic and precision-aware: fires only when both events share a
        `level_6_id`/`level_7_id` — so both reached street/place precision — and
        their date windows overlap. By default an **extracted** date is required;
        at the leaf level(s) in `det_publication_levels` (level 7 / place by
        default) a **publication-date** overlap is accepted too, since one
        specific place hosts ~one event of a type per day (this is what lets
        nameless incident reports, which usually lack an extracted date, merge).
        A coarse record (no fine id) never matches deterministically; it defers
        to the LLM. Type is already guaranteed by the partition. See
        `docs/todos/deterministic_match_slack.md` for the rationale and the
        accepted weaknesses (same-street collisions; coarse under-merge).
        """
        inc_fine = self._fine_id_set(prep.record.get("_geo") or {})
        if not inc_fine:
            return None
        inc_is_pub = prep.window.source != "extracted"
        if inc_is_pub and prep.window.start is None and prep.window.end is None:
            return None
        inc_s, inc_e = prep.window.start, prep.window.end
        pub_ok_levels = set(self.det_publication_levels)
        for cid in candidate_ids:
            cand = events.get(cid) or {}
            # Soft type: the partition may now span leaf event_types. The
            # deterministic (name-agnostic) merge only fires within the SAME
            # leaf event_type — a cross-type same-place/day pair (e.g. a concert
            # vs a festival at one venue) is referred to the LLM, never auto-merged.
            if cand.get("event_type") != prep.event_type:
                continue
            shared = inc_fine & self._fine_id_set(cand.get("_geo") or {})
            if not shared:
                continue
            cwin = self._cand_date_window(cand)
            if cwin is None:
                continue  # candidate has no date at all — can't confirm time
            cs, ce, cand_is_pub = cwin
            # If either side relies on a publication date, the shared id must be
            # at a leaf level we trust enough for a same-day merge.
            if inc_is_pub or cand_is_pub:
                shared_levels = {int(k.split(":", 1)[0][1:]) for k in shared}
                if not (shared_levels & pub_ok_levels):
                    continue
            if self._date_overlap(inc_s, inc_e, cs, ce, self.det_day_slack):
                return cid
        return None

    def _llm_adjudicate(
        self, prep: PreparedEvent, candidate_ids: Set[str], events: Dict[str, Dict[str, Any]],
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """Run the LLM disambiguator over a hard-capped candidate set.

        Returns `(match_id, llm_call)` where `llm_call` is a structured record
        of exactly what the LLM saw — the incoming payload, every candidate
        payload, and the pre/post-cap counts — for the case log and tuning.
        """
        pre_cap = len(candidate_ids)
        # Hard cap: never send more than `candidate_cap` candidates to the LLM.
        # When over the cap, keep the most recent (by publication date).
        kept: List[str] = list(candidate_ids)
        capped = self.candidate_cap is not None and pre_cap > self.candidate_cap
        if capped:
            kept = sorted(
                candidate_ids,
                key=lambda cid: str(events[cid].get("publication_date") or ""),
                reverse=True,
            )[: self.candidate_cap]

        inc_geo = prep.record.get("_geo") or {}
        incoming = _llm_payload(prep.record)
        candidate_records = [
            {
                "id": cid,
                **_llm_payload(events[cid]),
                "ubicacion_fina": self._leaf_relation(inc_geo, events[cid].get("_geo") or {}),
            }
            for cid in kept
        ]

        logger.info(
            "LLM disambiguation — event_type=%s partition=%s geo_key=%r: "
            "candidates pre_cap=%d sent=%d (cap=%s%s)",
            prep.event_type, prep.partition, prep.geo_key,
            pre_cap, len(candidate_records), self.candidate_cap,
            f", dropped {pre_cap - len(candidate_records)}" if capped else "",
        )
        logger.debug("  incoming: %s", incoming)
        for cr in candidate_records:
            logger.debug("  candidate %s: %s", cr["id"], cr)

        match_id = disambiguate(incoming, candidate_records)
        llm_call = {
            "candidate_cap": self.candidate_cap,
            "n_candidates_pre_cap": pre_cap,
            "n_candidates_sent": len(candidate_records),
            "n_dropped": pre_cap - len(candidate_records),
            "incoming": incoming,
            "candidates": candidate_records,
            "match_id": match_id,
        }
        return match_id, llm_call

    def adjudicate(
        self,
        prep: PreparedEvent,
        candidate_ids: Set[str],
        events: Dict[str, Dict[str, Any]],
    ) -> Tuple[Optional[str], str, List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Decide the match. Returns (match_id, path, candidate_debug, llm_call).

        `path ∈ {"no_candidates", "deterministic", "llm"}` records how the
        decision was reached — the deterministic gate skips the LLM call.
        `llm_call` is the structured payload sent to the LLM (incoming +
        candidates + cap counts) when `path == "llm"`, else None.
        """
        if not candidate_ids:
            return None, "no_candidates", [], None
        debug = self._candidate_debug(prep, candidate_ids, events)
        if self.deterministic_merge:
            det = self._deterministic_match(prep, candidate_ids, events)
            if det is not None:
                return det, "deterministic", debug, None
        match_id, llm_call = self._llm_adjudicate(prep, candidate_ids, events)
        return match_id, "llm", debug, llm_call

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
        self._register(linked, prep.partition, window, index)
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
        # geo/day-keys the merge introduced (location may have been promoted).
        # Old keys stay registered (the index is append-only), so recall never
        # shrinks.
        if self.bounded_merge_widening:
            # Register the incoming record's window directly; the canonical
            # range no longer widens, so it can't be used for reindexing.
            self._register(base, prep.partition, window, index)
        else:
            merged_s = _parse_dt(base_dr.get("start"))
            merged_e = _parse_dt(base_dr.get("end"))
            if merged_s or merged_e:
                merged_window = DateWindow(
                    merged_s, merged_e, self.extracted_slack_days, "extracted"
                )
                self._register(base, prep.partition, merged_window, index)
            else:
                self._register(base, prep.partition, None, index)

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
