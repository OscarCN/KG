"""Text helpers for the linker — operate on name strings only.

`name_similarity` is used by the deterministic merge gate as the *name* signal
(one of four: geo, name, type, date). It never touches geocoded data — it
compares two event `name` strings and returns a score in [0, 1].

Measure: **character-trigram Jaccard over the normalized, de-spaced string**
(lowercase + strip accents + keep `[a-z0-9]`). Token-set Jaccard was tried first
but failed the canonical case — "Mega Bachetón 2026" vs "MegaBacheton 2026"
scored only 0.25 because spacing/compounding splits the tokens. De-spaced
trigrams collapse that variation (→ 1.0) while staying conservative on genuinely
different names. The gate also requires coordinate proximity and date overlap,
so a slightly generous name measure can't merge alone.

Deliberately dependency-free (stdlib `unicodedata`): not worth a fuzzy-match or
embedding dependency for a gate this simple.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional, Set

_KEEP_RE = re.compile(r"[^a-z0-9]+")
_TRIGRAM_N = 3


def _normalize(text: Optional[str]) -> str:
    """Lowercase, strip accents, drop everything but `[a-z0-9]` (incl. spaces)."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _KEEP_RE.sub("", stripped.lower())


def _trigrams(s: str) -> Set[str]:
    if len(s) < _TRIGRAM_N:
        return {s} if s else set()
    return {s[i : i + _TRIGRAM_N] for i in range(len(s) - _TRIGRAM_N + 1)}


def name_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Character-trigram Jaccard of two names (de-spaced, accent-insensitive), in [0, 1].

    Returns 0.0 when either name is empty/None — the deterministic name branch
    only fires when both events are actually named, so an absent name can never
    contribute a spurious match. Strings shorter than a trigram fall back to
    exact equality.
    """
    na = _normalize(a)
    nb = _normalize(b)
    if not na or not nb:
        return 0.0
    if len(na) < _TRIGRAM_N or len(nb) < _TRIGRAM_N:
        return 1.0 if na == nb else 0.0
    ta = _trigrams(na)
    tb = _trigrams(nb)
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0
