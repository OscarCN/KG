"""Event linker — deduplicate extracted events using LLM disambiguation.

Pipeline (events only):

    new event → schema parse → strategy.prepare (geocode + window/keys)
              → candidate lookup (CandidateIndex)
              → LLM adjudication → match-id ? merge : create new

The supertype-specific behaviour (geo partitioning, date fallbacks, LLM
payload, merge policy) lives in `strategy.py` (`GeoEventStrategy`);
candidate storage lives behind the `CandidateIndex` protocol
(`index.py`). `EntityLinker` only parses the record envelope, selects a
strategy by schema category, and orchestrates the calls.

Themes and entities/concepts are declared skips — they have no strategy
registered yet and are tallied under `linker.dropped`.
"""

from __future__ import annotations

import copy
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional

from src.schema.parse_object import Parser
from src.schema.schemas.read_schema import load_schema

from .index import CandidateIndex, InMemoryCandidateIndex
from .strategy import build_strategies


LinkStatus = Literal["created", "merged", "skipped", "dropped", "error"]


@dataclass
class LinkResult:
    """Outcome of `EntityLinker.link_one(raw)`.

    `status`:
      - `"created"` — a new canonical event was minted (`event_id` set).
      - `"merged"`  — incoming record folded into existing event (`event_id` set).
      - `"skipped"` — record's category isn't linked yet (theme/entity); no event_id.
      - `"dropped"` — couldn't link (missing supertype, schema, type, or date); no event_id.
      - `"error"`   — exception during processing.
    `record` is the canonical post-link record when a link succeeded.
    """

    status: LinkStatus
    event_id: Optional[str] = None
    record: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None

logger = logging.getLogger(__name__)


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


def _category_for(supertype: str) -> Optional[str]:
    """Schema-declared category, or None when the supertype has no schema."""
    loaded = _get_schema(supertype)
    if not loaded:
        return None
    schema_key = _snake_to_pascal(supertype)
    return loaded.get("meta", {}).get(schema_key, {}).get("category", "event")


# ---------------------------------------------------------------------------
# EntityLinker — orchestration only; behaviour lives in the strategy
# ---------------------------------------------------------------------------


class EntityLinker:
    """Links extracted events into canonical event records via LLM disambiguation."""

    def __init__(
        self,
        geocode: bool = True,
        strategy_params: Optional[Dict[str, Any]] = None,
        index: Optional[CandidateIndex] = None,
    ):
        self.geocode = geocode

        # Linked events, keyed by minted id.
        self.events: Dict[str, Dict[str, Any]] = {}

        # Candidate retrieval backend (key construction is the strategy's job).
        self.index: CandidateIndex = index if index is not None else InMemoryCandidateIndex()

        # Category → strategy. Categories with no entry are declared skips.
        self._strategies = build_strategies(geocode=geocode, strategy_params=strategy_params)

        # Drop counters for the run summary.
        self.dropped: Dict[str, int] = defaultdict(int)

    # -- Public API ----------------------------------------------------

    def link_all(self, records: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        records = list(records)
        logger.debug("Starting link_all over %d records", len(records))
        for raw in records:
            self.link_one(raw)
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

    def link_one(self, raw: Dict[str, Any]) -> LinkResult:
        """Link a single extracted record. Used by the streaming runner.

        Wraps `_process` with a try/except so the streaming caller doesn't
        have to handle exceptions itself.
        """
        try:
            return self._process(raw)
        except Exception as ex:
            logger.exception(
                "Failed to link record (%s): %s", raw.get("_supertype"), ex
            )
            self.dropped["error"] += 1
            return LinkResult(status="error", reason=str(ex))

    def _process(self, raw: Dict[str, Any]) -> LinkResult:
        supertype = raw.get("_supertype")
        if not supertype:
            self.dropped["no_supertype"] += 1
            return LinkResult(status="dropped", reason="no_supertype")

        category = _category_for(supertype)
        if category is None:
            logger.warning("No schema found for supertype %r — dropping record", supertype)
            self.dropped["no_schema"] += 1
            return LinkResult(status="dropped", reason="no_schema")

        strategy = self._strategies.get(category)
        if strategy is None:
            self.dropped[f"skipped_category:{category}"] += 1
            return LinkResult(status="skipped", reason=f"category:{category}")

        record = self._normalize_envelope(raw, supertype)

        prep, drop_reason = strategy.prepare(record)
        if prep is None:
            self.dropped[drop_reason] += 1
            return LinkResult(status="dropped", reason=drop_reason)

        candidate_ids = self.index.lookup(strategy.lookup_keys(prep))
        match_id = strategy.adjudicate(prep, candidate_ids, self.events)

        if match_id and match_id in self.events:
            base = self.events[match_id]
            logger.debug(
                "MERGE — incoming source=%s date_source=%s name=%r desc=%r\n"
                "        into id=%s name=%r existing_sources=%d desc=%r",
                record.get("_source_id"),
                prep.window.source,
                record.get("name"),
                (record.get("description") or "")[:140],
                match_id,
                base.get("name"),
                len(base.get("source_ids") or []),
                (base.get("description") or "")[:140],
            )
            strategy.merge(base, prep, self.index)
            return LinkResult(status="merged", event_id=match_id, record=base)

        logger.debug(
            "CREATE — event_type=%s geo_key=%r date_source=%s "
            "dates=%s..%s slack=%dd name=%r desc=%r (no match among %d candidates)",
            prep.event_type,
            prep.geo_key,
            prep.window.source,
            prep.window.start.isoformat() if prep.window.start else None,
            prep.window.end.isoformat() if prep.window.end else None,
            prep.window.slack_days,
            record.get("name"),
            (record.get("description") or "")[:140],
            len(candidate_ids),
        )
        new_id, linked = strategy.create(prep, self.index)
        self.events[new_id] = linked
        return LinkResult(status="created", event_id=new_id, record=linked)

    # -- Envelope -------------------------------------------------------

    def _normalize_envelope(self, raw: Dict[str, Any], supertype: str) -> Dict[str, Any]:
        """Schema-parse the payload, then re-attach the provenance fields.

        The envelope fields (`_source_id`, `_supertype`, `date_created`) ride
        on extracted records but are not part of the supertype schema, so
        they're stripped before parsing and merged back after.
        """
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
        return record

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
