"""Static catalogue of the 32 Mexican states for geo-partition keys.

Used by the geo-event linking strategy in two places:

- to normalize the geocoder's `level_2` value into a stable partition
  key (accent/spelling variants collapse to one canonical slug), and
- as the *fallback* tier when geocoding yields no state but the
  extracted `location.state` text names one — deterministic, no
  service call.

`normalize_state(text)` returns the canonical slug for a recognized
state name/alias, or None. `slug(text)` is the plain normalizer
(lowercase, accent-stripped, punctuation removed) used as a last
resort for unrecognized geocoder values so the key space stays
consistent.
"""

from __future__ import annotations

import unicodedata
from typing import Dict, Optional, Tuple


def slug(text: Optional[str]) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text.strip().lower())
    cleaned = "".join(
        c if c.isalnum() or c.isspace() else " "
        for c in decomposed
        if not unicodedata.combining(c)
    )
    return " ".join(cleaned.split())


# Canonical slug → accepted aliases (already in slug form).
_STATES: Dict[str, Tuple[str, ...]] = {
    "aguascalientes": ("ags",),
    "baja california": ("bc", "baja california norte"),
    "baja california sur": ("bcs",),
    "campeche": ("camp",),
    "coahuila": ("coahuila de zaragoza", "coah"),
    "colima": (),
    "chiapas": ("chis",),
    "chihuahua": ("chih",),
    "ciudad de mexico": ("cdmx", "df", "distrito federal", "mexico city", "mexico df"),
    "durango": ("dgo",),
    "guanajuato": ("gto",),
    "guerrero": ("gro",),
    "hidalgo": ("hgo",),
    "jalisco": ("jal",),
    "estado de mexico": ("mexico", "edomex", "edo de mexico", "estado de mexico edomex"),
    "michoacan": ("michoacan de ocampo", "mich"),
    "morelos": ("mor",),
    "nayarit": ("nay",),
    "nuevo leon": ("nl",),
    "oaxaca": ("oax",),
    "puebla": ("pue",),
    "queretaro": ("qro", "queretaro de arteaga"),
    "quintana roo": ("qroo", "q roo"),
    "san luis potosi": ("slp",),
    "sinaloa": ("sin",),
    "sonora": ("son",),
    "tabasco": ("tab",),
    "tamaulipas": ("tamps",),
    "tlaxcala": ("tlax",),
    "veracruz": ("veracruz de ignacio de la llave", "ver"),
    "yucatan": ("yuc",),
    "zacatecas": ("zac",),
}

_ALIASES: Dict[str, str] = {}
for _canonical, _alias_list in _STATES.items():
    _ALIASES[_canonical] = _canonical
    for _alias in _alias_list:
        _ALIASES[_alias] = _canonical


def normalize_state(text: Optional[str]) -> Optional[str]:
    """Return the canonical state slug for a name/alias, or None."""
    return _ALIASES.get(slug(text))
