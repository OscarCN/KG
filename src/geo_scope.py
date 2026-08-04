"""Geographic scope filter over enriched documents (`FILTER_GEO`).

Consumer-side geo pre-scope for the demo (and likely a while beyond): the
producer streams the full post-gp3 firehose, and the listener drops documents
outside the configured areas before keyword matching / extraction. The same
rule drives the backfill producer's per-document filter
(`scripts/enqueue_from_es.py`), which additionally coarse-fetches ES by the
covering states.

Configuration is a comma-separated list of geoid prefixes in `FILTER_GEO`
(leading underscores optional), mixing granularities freely, e.g.::

    FILTER_GEO=_48409014,_48409015,_48409016,_48422,_48402

A document is in scope iff ANY `locations_mentioned` entry matches ANY prefix:

- **Prefix finer than state** (more digits than a level_2 id, e.g. municipio
  `48409014`): the entry's `geoid` starts with the prefix. The prefix depth
  itself guarantees municipality-or-finer granularity.
- **State-or-coarser prefix** (e.g. `48422`): the entry's `geoid` starts with
  the prefix (or its `level_2_id` equals it), AND the entry resolves at
  `precision_level >= 3` (city or finer) — so a bare state mention
  ("Querétaro") does not put a document in scope.

Unset/empty `FILTER_GEO` disables the filter (the listener processes the whole
stream).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional

# Demo scope: CDMX municipios Benito Juárez / Cuauhtémoc / Miguel Hidalgo
# (48409 014/015/016), all of Querétaro (48422), all of Baja California
# (48402). Default for the backfill producer; the listener only filters when
# FILTER_GEO is set explicitly.
DEMO_FILTER_GEO = "_48409014,_48409015,_48409016,_48422,_48402"

# Digits of a level_2 (state) id, e.g. `48422` — country 484 + 2-digit state.
_STATE_ID_DIGITS = 5

# Minimum precision_level for mentions matched via a state-or-coarser prefix.
MIN_PRECISION = 3


def _norm_id(value: Any) -> str:
    return str(value or "").lstrip("_").strip()


def _precision(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


class GeoScope:
    """Prefix-based geo scope over `locations_mentioned` entries."""

    def __init__(self, prefixes: Iterable[str]):
        self.prefixes = tuple(
            p for p in (_norm_id(raw) for raw in prefixes) if p
        )
        if not self.prefixes:
            raise ValueError("GeoScope requires at least one geoid prefix")

    @classmethod
    def from_env(cls, default: Optional[str] = None) -> Optional["GeoScope"]:
        """Build from `FILTER_GEO` (fallback `default`); None = no filtering."""
        raw = os.environ.get("FILTER_GEO") or default or ""
        prefixes = [p for p in (s.strip() for s in raw.split(",")) if p]
        return cls(prefixes) if prefixes else None

    def matches_entry(self, loc: Dict[str, Any]) -> bool:
        geoid = _norm_id(loc.get("geoid"))
        level_2_id = _norm_id(loc.get("level_2_id"))
        for prefix in self.prefixes:
            if len(prefix) > _STATE_ID_DIGITS:
                if geoid and geoid.startswith(prefix):
                    return True
            elif (
                geoid.startswith(prefix) or level_2_id == prefix
            ) and _precision(loc.get("precision_level")) >= MIN_PRECISION:
                return True
        return False

    def matches_doc(self, doc: Dict[str, Any]) -> bool:
        """True iff any `locations_mentioned` entry is in scope."""
        return any(
            self.matches_entry(loc)
            for loc in (doc.get("locations_mentioned") or [])
            if isinstance(loc, dict)
        )

    def covering_level2s(self) -> List[str]:
        """The level_2 (state) ids covering every prefix — for coarse ES
        `cvegeo` pre-filtering by the backfill producer."""
        return sorted({p[:_STATE_ID_DIGITS] for p in self.prefixes})

    def __repr__(self) -> str:
        return f"GeoScope(prefixes={self.prefixes})"
