"""Layer-aware, outlier-robust aggregation of event date windows.

Single source of truth for turning a set of per-source date windows into one
canonical date range. Shared by the reconciliation dry-run / merge primitive and
the linker's ``_apply_best_window`` (which delegates its precision ranking here).

A canonical event's date depends on its **layer**:

- ``instance`` — the sources are noisy views of one point occurrence, so the
  canonical date is the **narrowest** window (smallest effective precision) of the
  dominant cluster.
- ``umbrella`` — the event genuinely spans a period (a festival, a tournament, a
  season), so the canonical date is the **envelope** (min-start … max-end) of the
  dominant cluster.

Both first take the **dominant date cluster**, which drops swapped/placeholder-date
outliers (the day↔month swaps, ``2023``/``1990`` placeholders) that would otherwise
empty an intersection or explode an envelope.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

# (start, end, precision_days) — end/precision_days may be None.
Window = Tuple[Optional[datetime], Optional[datetime], Optional[int]]


def effective_precision_days(start: Optional[datetime], end: Optional[datetime],
                             precision_days: Optional[int]) -> float:
    """Window narrowness in days — smaller = more precise.

    ``precision_days`` when present is authoritative. When **absent** it means
    *unknown*, not *exact*: fall back to the window **width** (``end - start``), and
    for a start-only window return ``inf`` so it never wins as most-precise.
    """
    if precision_days is not None:
        try:
            return float(int(precision_days))
        except (TypeError, ValueError):
            pass
    if start and end:
        return max(0.0, (end.date() - start.date()).days)
    return float("inf")


def dominant_window_cluster(windows: List[Window], span_days: int = 45) -> List[Window]:
    """Greedy-cluster windows by center date; return the largest cluster.

    Drops swapped/placeholder-date outliers — a window whose center is more than
    ``span_days`` from the dominant run of sources is left out.
    """
    dated = [(s, e or s, pd) for s, e, pd in windows if s]
    if len(dated) <= 1:
        return dated
    centered = sorted(dated, key=lambda w: (w[0].timestamp() + w[1].timestamp()) / 2)
    clusters: List[List[Tuple]] = []
    for w in centered:
        c = (w[0].timestamp() + w[1].timestamp()) / 2
        if clusters and abs(c - clusters[-1][-1][3]) <= span_days * 86400:
            clusters[-1].append((*w, c))
        else:
            clusters.append([(*w, c)])
    best = max(clusters, key=len)
    return [(s, e, pd) for s, e, pd, _c in best]


def aggregate_date(windows: List[Window], layer: str, span_days: int = 45
                   ) -> Tuple[Optional[datetime], Optional[datetime], Optional[int], int]:
    """Return ``(start, end, precision_days, n_outliers_dropped)`` for the layer.

    ``umbrella`` → envelope of the dominant cluster (``precision_days`` None).
    ``instance`` → the narrowest window of the dominant cluster (keeps its
    ``precision_days``).
    """
    dom = dominant_window_cluster(windows, span_days)
    dropped = sum(1 for s, _e, _pd in windows if s) - len(dom)
    if not dom:
        return None, None, None, dropped
    if layer == "umbrella":
        start = min(s for s, _e, _pd in dom)
        end = max(e for _s, e, _pd in dom)
        return start, end, None, dropped
    s, e, pd = min(dom, key=lambda w: effective_precision_days(w[0], w[1], w[2]))
    return s, e, pd, dropped
