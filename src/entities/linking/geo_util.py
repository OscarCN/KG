"""Geographic helpers for the linker — operate on coordinates only.

Two distinct granularities, deliberately separate:

- `grid_cell` / `grid_neighbors` — a cheap lat/lon grid used as a **retrieval**
  bucket (broad recall, ~1 km cells). Two events in nearby cells are co-retrieved
  as candidates; the precise same-place test happens later.
- `haversine` — metric distance (meters) used by the **deterministic merge gate**
  (precision). This is the actual "same street/place?" measure, robust across the
  administrative-boundary disagreements that make `level_N_id` equality brittle.

Neither function touches names, types, or dates — only `(lat, lon)`.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

GridCell = Tuple[int, int]

# Mean Earth radius (meters).
_EARTH_RADIUS_M = 6_371_000.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in meters."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def grid_cell(lat: Optional[float], lon: Optional[float], size_deg: float) -> Optional[GridCell]:
    """Snap a coordinate to an integer grid cell of side `size_deg` degrees.

    Returns None when either coordinate is missing, so callers can skip the
    coordinate retrieval path for unresolved locations.
    """
    if lat is None or lon is None:
        return None
    return (math.floor(lat / size_deg), math.floor(lon / size_deg))


def grid_neighbors(cell: GridCell) -> List[GridCell]:
    """The 3×3 block of cells centred on `cell` (the cell itself + 8 neighbors)."""
    r, c = cell
    return [(r + dr, c + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)]
