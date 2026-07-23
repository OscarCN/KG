"""Canonical<->canonical reconciliation — READ-ONLY dry run.

Finds already-canonical events in kgdb that should be a single event (the twin
leak in ``docs/todos/canonical_reconciliation.md``) and prints the proposed
merges. **Writes nothing.** This is the diagnostic that validates the name-led
reconciliation hypothesis on real data before any merge primitive is built.

Approach (see docs/todos/canonical_reconciliation.md + retrieval_name_soft_type.md):

  Retrieval (candidate pairs, same supertype), union of four paths:
    1. shared level_7_id (same venue/place)
    2. shared level_6_id (same street)
    3. coordinate proximity  (0.003 deg grid + 8 neighbors, haversine <= PROX_M)
    4. coarse->fine bridge    (a record with no level_6/7 shares its deepest
                               admin id -- level_3_id else level_2_id -- with a
                               finer record whose id-path contains it)

  Decision (name-led, NOT date-led):
    propose merge edge  iff  geo-compatible (per-level id containment; no
    contradiction at any shared level)  AND  name_similarity >= NAME_MIN  AND
    not a precision-aware date reject (reject only when BOTH sides carry a
    *precise* date -- small precision_days -- and their windows are disjoint
    beyond slack; the widened event_properties window is deliberately NOT used).

  Union-find over edges -> merge groups. A group larger than FANOUT_WARN is
  flagged (stop-and-inspect, not auto-merge): the busy-venue over-merge guard.

Read from the same kgdb the writer uses (KGDB_* env, .env.local -> dev :5334).

Usage:
    python scripts/reconcile_dryrun.py
    RECON_SUPERTYPE=paid_mass_event python scripts/reconcile_dryrun.py
    RECON_NAME_MIN=0.5 RECON_LEVEL7=_4842201400010181000020001 \
        python scripts/reconcile_dryrun.py     # focus one venue
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from dateutil import parser as dtparser
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
load_dotenv(_PROJECT_ROOT / ".env.local")

from src.entities.linking.aggregate import aggregate_date, dominant_window_cluster  # noqa: E402
from src.entities.linking.geo_util import grid_cell, grid_neighbors, haversine  # noqa: E402
from src.entities.linking.text_util import _normalize as _norm_name  # noqa: E402
from src.entities.linking.text_util import name_similarity  # noqa: E402

# --- tunables -----------------------------------------------------------------
NAME_MIN = float(os.environ.get("RECON_NAME_MIN", "0.55"))  # merge edge name threshold
PROX_M = float(os.environ.get("RECON_PROX_M", "150"))       # proximity edge, meters
GRID_DEG = 0.003                                            # ~330 m cells
PRECISE_DAYS = int(os.environ.get("RECON_PRECISE_DAYS", "3"))  # <=this => date is "precise"
DATE_SLACK_DAYS = 1                                         # base overlap slack (days)
FANOUT_WARN = int(os.environ.get("RECON_FANOUT_WARN", "6")) # flag groups bigger than this
CONTAIN_MIN_CHARS = int(os.environ.get("RECON_CONTAIN_MIN", "6"))  # specificity guard
CONTAIN_SCORE = 0.90                                        # score for a contained-name hit
HIGH_TRIGRAM = float(os.environ.get("RECON_HIGH_TRIGRAM", "0.75"))  # auto-merge (no LLM) name floor
USE_LLM = os.environ.get("RECON_LLM") == "1"               # escalate borderline groups to the LLM
_ADMIN_LEVELS = (1, 2, 3, 5, 6, 7)  # level 4 unused


def name_score(a: Optional[str], b: Optional[str]) -> float:
    """Trigram Jaccard, plus a containment bonus for a short specific name embedded
    in a longer one ("Zona Fest" in "MéxiCQ Zona Fest"). The bonus fires only when
    the contained normalized string is >= CONTAIN_MIN_CHARS (low-IDF/short-name
    guard); the geo hard-gate + date reject still scope it to the same place+time."""
    base = name_similarity(a, b)
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return base
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= CONTAIN_MIN_CHARS and shorter in longer:
        return max(base, CONTAIN_SCORE)
    return base


_ADJ_MODEL = os.environ.get("OPENROUTER_LINKER_MODEL", "google/gemini-2.5-flash-lite")
_ADJ_CACHE = _PROJECT_ROOT / "cache" / "reconcile_llm"

_ADJ_SYSTEM = (
    "Eres un modelo de reconciliación de eventos. Recibes COMPONENTES; cada uno "
    "contiene UNIDADES ya consolidadas (una unidad = uno o más reportes del MISMO "
    "hecho, con nombre casi idéntico, mismo lugar y fechas compatibles — ya están "
    "bien agrupados, NO los dividas). Para CADA componente: (1) AGRUPA sus unidades "
    "en los eventos reales DISTINTOS, y (2) ETIQUETA la capa de cada evento.\n\n"
    "Reglas de agrupación:\n"
    "- FUSIONA unidades que son la MISMA ocurrencia o el MISMO paraguas: variantes "
    "de marca/redacción del mismo festival o torneo ('Zona Fest' y 'MéxiCQ Zona "
    "Fest' y 'ZonaFest Estadio Corregidora'), o calificativos meramente descriptivos "
    "('Amistoso México vs Portugal' = 'México vs Portugal').\n"
    "- MANTÉN SEPARADAS: (a) un PARAGUAS (festival, Mundial, torneo, feria, "
    "temporada) frente a sus INSTANCIAS específicas (un concierto con su artista, un "
    "partido concreto, una inauguración); (b) instancias HERMANAS entre sí ('Final "
    "M-17' vs 'Final M-20'; el mismo tour en Puebla vs en Querétaro; dos partidos "
    "distintos).\n"
    "- NO separes por diferencias de FECHA ni de PRECISIÓN de ubicación: pueden "
    "venir mal parseadas (día/mes invertido, p.ej. 2026-11-06 por 2026-06-11) o con "
    "distinta granularidad geográfica. Júzgalo por los HECHOS y el nombre. "
    "'event_type' es señal SUAVE (un mismo evento puede venir como concert/festival/"
    "party).\n"
    "- REGLA CLAVE: pertenecer al MISMO paraguas NO es lo mismo que ser el MISMO "
    "evento. Un sub-evento con identidad propia (un concierto con artista nombrado, "
    "una final/partido específico, una inauguración) es su PROPIO evento 'instance' "
    "y NO se fusiona con su paraguas ni con sus hermanos — quedan como eventos "
    "SEPARADOS (después se enlazarán como padre/hijo). El evento paraguas agrupa "
    "SOLO los reportes genéricos del contenedor (sin sub-evento específico). "
    "Ejemplo: 'Olimpiada Nacional' es un evento; 'Final M-17', 'Final M-20 varonil' "
    "y 'Final femenil M-20' son TRES eventos instance más, distintos entre sí y del "
    "paraguas.\n"
    "- ETIQUETA cada evento con 'layer': 'umbrella' SOLO si el evento en sí es un "
    "contenedor de varias sub-ocurrencias distintas o un periodo largo (un festival "
    "de varios días con distintos actos, el Mundial, una feria, una temporada, una "
    "olimpiada, una gira). Un ÚNICO partido, concierto o función —aunque se reporte "
    "muchas veces— es 'instance', NO umbrella.\n\n"
    "Responde EXCLUSIVAMENTE en JSON de la forma:\n"
    '{"components": [{"component_id": <n>, "events": [{"units": [<uid>,<uid>], '
    '"layer": "umbrella"}]}]}\n'
    "Cada unit_id de un componente debe aparecer EXACTAMENTE una vez."
)


def _unit_repr(unit_idxs: List[int], events: List[Dict[str, Any]], uid: int) -> Dict[str, Any]:
    """Compact summary of a deterministic unit for the LLM (representative =
    the record with the most sources; date = the unit's envelope)."""
    rep = max(unit_idxs, key=lambda i: events[i]["n_sources"])
    names = sorted({events[i]["name"] for i in unit_idxs if events[i]["name"]})[:4]
    starts = [events[i]["start"] for i in unit_idxs if events[i]["start"]]
    ends = [events[i]["end"] or events[i]["start"] for i in unit_idxs if events[i]["start"]]
    venue = events[rep]["ids"].get(7) or events[rep]["ids"].get(6) or events[rep]["ids"].get(3)
    return {
        "unit_id": uid,
        "name": events[rep]["name"] or "(sin nombre)",
        "names": names,
        "event_type": events[rep]["event_type"],
        "description": events[rep]["description"][:200],
        "venue": venue,
        "date": {"start": min(starts).isoformat() if starts else None,
                 "end": max(ends).isoformat() if ends else None},
        "n_records": len(unit_idxs),
    }


def _comp_cache_key(unit_reprs: List[Dict[str, Any]]) -> str:
    blob = json.dumps({"model": _ADJ_MODEL, "prompt": _ADJ_SYSTEM, "units": unit_reprs},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse_components(raw: str) -> Optional[List[Dict[str, Any]]]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj.get("components") if isinstance(obj, dict) else None


def _apply_component(comp: Optional[Dict[str, Any]], uids: List[int]
                     ) -> Optional[List[Tuple[List[int], str]]]:
    """Map one component's LLM result to [(unit_id_list, layer), ...]. None if
    malformed or the unit-set doesn't exactly cover the component's units."""
    if not comp or not isinstance(comp.get("events"), list):
        return None
    out: List[Tuple[List[int], str]] = []
    seen: set = set()
    valid = set(uids)
    for ev in comp["events"]:
        if not isinstance(ev, dict) or not isinstance(ev.get("units"), list):
            return None
        group = []
        for u in ev["units"]:
            try:
                u = int(u)
            except (TypeError, ValueError):
                return None
            if u not in valid or u in seen:
                return None
            seen.add(u)
            group.append(u)
        if group:
            layer = ev.get("layer") if ev.get("layer") in ("umbrella", "instance") else "instance"
            out.append((group, layer))
    if seen != valid:
        return None
    return out


def adjudicate_components(llm_comps: List[Tuple[List[int], List[List[int]]]],
                          units: List[List[int]], events: List[Dict[str, Any]]
                          ) -> Dict[int, List[Tuple[List[int], str]]]:
    """For each multi-unit component, cluster its units into events + tag layer.

    `llm_comps[k] = (uids, _)`; returns {k: [(uid_list, layer), ...]}. Cached per
    component (hash of its unit reprs); one batched LLM call for cache-missing ones.
    A malformed/absent result falls back to merging all units as one instance."""
    from src.llm.openrouter import call_openrouter

    _ADJ_CACHE.mkdir(parents=True, exist_ok=True)
    reprs = [[_unit_repr(units[u], events, u) for u in uids] for uids, _ in llm_comps]
    keys = [_comp_cache_key(r) for r in reprs]

    result: Dict[int, List[Tuple[List[int], str]]] = {}
    pending: List[int] = []
    for k in range(len(llm_comps)):
        path = _ADJ_CACHE / f"{keys[k]}.json"
        if path.exists():
            comps = _parse_components(json.load(open(path, encoding="utf-8")).get("response", ""))
            first = comps[0] if comps else None
            parsed = _apply_component(first, llm_comps[k][0])
            if parsed is not None:
                result[k] = parsed
                continue
        pending.append(k)

    if pending:
        batch = [{"component_id": k, "units": reprs[k]} for k in pending]
        user = ("Agrupa las unidades de cada componente en eventos distintos y "
                "etiqueta la capa.\n\n"
                + json.dumps({"components": batch}, ensure_ascii=False, indent=1))
        try:
            raw = call_openrouter(
                [{"role": "system", "content": _ADJ_SYSTEM},
                 {"role": "user", "content": user}],
                model=_ADJ_MODEL, response_format={"type": "json_object"}, temperature=0.0)
        except Exception as ex:  # noqa: BLE001
            print(f"  LLM call failed: {ex}")
            raw = ""
        comps = _parse_components(raw) or []
        by_cid = {c.get("component_id"): c for c in comps if isinstance(c, dict)}
        for k in pending:
            parsed = _apply_component(by_cid.get(k), llm_comps[k][0])
            result[k] = parsed if parsed is not None else [(llm_comps[k][0], "instance")]
            slice_resp = json.dumps({"components": [by_cid.get(k)]}) if by_cid.get(k) else ""
            with open(_ADJ_CACHE / f"{keys[k]}.json", "w", encoding="utf-8") as f:
                json.dump({"response": slice_resp, "model": _ADJ_MODEL}, f, ensure_ascii=False)
    return result


# --- Layer-aware, outlier-robust aggregation (date logic in linking/aggregate.py) --

def aggregate_event(record_idxs: List[int], layer: str, events: List[Dict[str, Any]]
                    ) -> Dict[str, Any]:
    """Layer-aware, outlier-robust date+location for a reconciled event.

    instance → narrowest window of the dominant cluster + finest single venue.
    umbrella → envelope (min-start..max-end) of the dominant cluster + venue set."""
    windows = [(events[i]["start"], events[i]["end"], events[i]["precision_days"])
               for i in record_idxs]
    start, end, pd, n_out = aggregate_date(windows, layer)
    date = {"start": start.date().isoformat() if start else None,
            "end": end.date().isoformat() if end else None,
            "precision_days": pd}

    if layer == "umbrella":
        venues = sorted({events[i]["ids"].get(7) or events[i]["ids"].get(6)
                         for i in record_idxs if events[i]["ids"].get(7) or events[i]["ids"].get(6)})
        loc = {"venues": venues}
    else:
        fine = max(record_idxs, key=lambda i: max([0] + [n for n in _ADMIN_LEVELS
                                                         if events[i]["ids"].get(n)]))
        loc = {"venue": events[fine]["ids"].get(7) or events[fine]["ids"].get(6)
               or events[fine]["ids"].get(3) or events[fine]["ids"].get(2)}
    return {"layer": layer, "date": date, "location": loc,
            "n_records": len(record_idxs),
            "n_sources": sum(events[i]["n_sources"] for i in record_idxs),
            "outliers_dropped": n_out}


# --- Sibling-discriminator guard --------------------------------------------------
# Two names are SIBLINGS (distinct instances that must never merge) when they share a
# stem but each carries a distinct *discriminator*: a different NUMBER (Final M-17 vs
# M-20, 5a vs 3a fecha) or a different GENDER category (varonil vs femenil). Kept
# deliberately narrow — synonym variation ("Patrimonio Mundial" vs "Patrimonio de la
# Humanidad") has no number/gender split, so it is NOT flagged and still merges.
# Proper-noun-opponent siblings (vs Serbia / vs Sudáfrica) are left to geo/date + LLM.

_GENDER = {"varonil": "m", "masculino": "m", "masculina": "m", "varonial": "m",
           "femenil": "f", "femenino": "f", "femenina": "f"}


def _sibling_tokens(name: str) -> Tuple[set, set, set]:
    """(alpha tokens, number tokens, gender classes) from a name (accent-insensitive)."""
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFKD", (name or "").lower())
                if not unicodedata.combining(c))
    alpha = set(re.findall(r"[a-z]+", s))
    nums = set(re.findall(r"\d+", s))
    gender = {_GENDER[t] for t in alpha if t in _GENDER}
    return alpha, nums, gender


def _are_siblings(a: str, b: str) -> bool:
    ta, na, ga = _sibling_tokens(a)
    tb, nb, gb = _sibling_tokens(b)
    if not ta or not tb:
        return False
    num_sib = bool(na and nb and (na - nb) and (nb - na))       # both numbered, differ
    gender_sib = bool(ga and gb and ga != gb)                   # both gendered, differ
    if not (num_sib or gender_sib):
        return False
    stem = (ta & tb) - set(_GENDER)                             # shared, non-discriminator
    return len(stem) >= 2                                       # clearly the same family


def _connect():
    return psycopg2.connect(
        host=os.environ["KGDB_HOST"],
        port=int(os.environ.get("KGDB_PORT", 5432)),
        user=os.environ["KGDB_USER"],
        password=os.environ["KGDB_PASSWORD"],
        dbname=os.environ["KGDB_NAME"],
    )


def _load_events(conn, supertype: Optional[str]) -> List[Dict[str, Any]]:
    """One row per canonical event with name/date/geo pulled from metadata."""
    # Exclude tombstoned entities (already merged away) so the sweep is idempotent —
    # they're invisible to linker retrieval (no event_properties) and must be here too.
    where = ("WHERE e.metadata->>'_supertype' IS NOT NULL "
             "AND e.metadata->>'_merged_into' IS NULL")
    params: List[Any] = []
    if supertype:
        where += " AND e.metadata->>'_supertype' = %s"
        params.append(supertype)
    sql = f"""
        SELECT e.entity_id, e.metadata,
               COALESCE(jsonb_array_length((e.metadata->'source_ids')::jsonb), 0) AS n_sources
        FROM entities e
        {where}
    """
    out: List[Dict[str, Any]] = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            meta = row["metadata"]
            if isinstance(meta, str):
                import json
                meta = json.loads(meta)
            geo = meta.get("_geo") or {}
            dr = ((meta.get("date_range") or {}).get("date_range")) or {}
            ids = {n: (geo.get(f"level_{n}_id") or None) for n in _ADMIN_LEVELS}
            lat, lon = geo.get("matched_lat"), geo.get("matched_lon")
            out.append({
                "id": row["entity_id"],
                "supertype": meta.get("_supertype"),
                "name": meta.get("name") or "",
                "description": meta.get("description") or "",
                "location": meta.get("location") or {},
                "event_type": meta.get("event_type"),
                "n_sources": row["n_sources"],
                "ids": ids,                     # {level: id or None}
                "lat": float(lat) if lat is not None else None,
                "lon": float(lon) if lon is not None else None,
                "start": _parse_dt(dr.get("start")),
                "end": _parse_dt(dr.get("end")) or _parse_dt(dr.get("start")),
                "precision_days": _to_int((meta.get("date_range") or {}).get("precision_days")),
                "deepest": _deepest_level(ids),
            })
    return out


def _parse_dt(v):
    if not v:
        return None
    try:
        return dtparser.parse(v)
    except (ValueError, TypeError):
        return None


def _to_int(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _deepest_level(ids: Dict[int, Optional[str]]) -> Optional[int]:
    for n in reversed(_ADMIN_LEVELS):
        if ids.get(n):
            return n
    return None


def _geo_compatible(a: Dict, b: Dict) -> bool:
    """One id-path contained in the other: equal at every shared non-null level,
    and at least one shared non-null level (noloc is compatible with nothing)."""
    shared = 0
    for n in _ADMIN_LEVELS:
        ia, ib = a["ids"].get(n), b["ids"].get(n)
        if ia and ib:
            if ia != ib:
                return False
            shared += 1
    return shared > 0


def _date_rejects(a: Dict, b: Dict) -> bool:
    """True only when both dates are precise and disjoint beyond slack."""
    if not (a["start"] and b["start"]):
        return False  # a missing date is non-informative, never a reject
    pa, pb = a["precision_days"], b["precision_days"]
    if pa is None or pb is None or pa > PRECISE_DAYS or pb > PRECISE_DAYS:
        return False  # at least one side imprecise -> date can't discriminate
    slack = (DATE_SLACK_DAYS + max(pa, pb)) * 86400
    a0, a1 = sorted((a["start"].timestamp(), a["end"].timestamp()))
    b0, b1 = sorted((b["start"].timestamp(), b["end"].timestamp()))
    return (a0 - b1 > slack) or (b0 - a1 > slack)  # gap exceeds slack => different


def _candidate_pairs(events: List[Dict]) -> set:
    """Union of the four retrieval paths, as a set of (i, j) index pairs, i<j."""
    pairs: set = set()

    def add(i: int, j: int):
        pairs.add((i, j) if i < j else (j, i))

    by_super: Dict[str, List[int]] = defaultdict(list)
    for idx, e in enumerate(events):
        by_super[e["supertype"]].append(idx)

    for _st, idxs in by_super.items():
        by_l7: Dict[str, List[int]] = defaultdict(list)
        by_l6: Dict[str, List[int]] = defaultdict(list)
        by_grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        coarse: List[int] = []
        by_l3: Dict[str, List[int]] = defaultdict(list)
        by_l2: Dict[str, List[int]] = defaultdict(list)
        for idx in idxs:
            e = events[idx]
            if e["ids"].get(7):
                by_l7[e["ids"][7]].append(idx)
            if e["ids"].get(6):
                by_l6[e["ids"][6]].append(idx)
            cell = grid_cell(e["lat"], e["lon"], GRID_DEG)
            if cell is not None:
                by_grid[cell].append(idx)
            if e["ids"].get(3):
                by_l3[e["ids"][3]].append(idx)
            if e["ids"].get(2):
                by_l2[e["ids"][2]].append(idx)
            if not e["ids"].get(6) and not e["ids"].get(7):
                coarse.append(idx)

        # Path 1 & 2: shared venue / street
        for group in list(by_l7.values()) + list(by_l6.values()):
            for a in range(len(group)):
                for b in range(a + 1, len(group)):
                    add(group[a], group[b])

        # Path 3: coordinate proximity (grid cell + 8 neighbors, then haversine)
        for idx in idxs:
            e = events[idx]
            cell = grid_cell(e["lat"], e["lon"], GRID_DEG)
            if cell is None:
                continue
            for nb in grid_neighbors(cell):
                for j in by_grid.get(nb, ()):
                    if j <= idx:
                        continue
                    f = events[j]
                    if haversine(e["lat"], e["lon"], f["lat"], f["lon"]) <= PROX_M:
                        add(idx, j)

        # Path 4: coarse -> fine bridge (coarse shares deepest admin id with finer)
        for idx in coarse:
            e = events[idx]
            key_level = 3 if e["ids"].get(3) else (2 if e["ids"].get(2) else None)
            if key_level is None:
                continue
            bucket = (by_l3 if key_level == 3 else by_l2)[e["ids"][key_level]]
            for j in bucket:
                if j != idx:
                    add(idx, j)

    return pairs


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _local_units(comp: List[int], strict_adj: Dict[int, set]) -> List[List[int]]:
    """Partition a component into deterministic units via strict edges restricted
    to the component (so a unit never crosses the venue-purity split)."""
    cset = set(comp)
    parent = {x: x for x in comp}

    def f(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in comp:
        for j in strict_adj.get(i, ()):
            if j in cset:
                ri, rj = f(i), f(j)
                if ri != rj:
                    parent[ri] = rj
    groups: Dict[int, List[int]] = defaultdict(list)
    for i in comp:
        groups[f(i)].append(i)
    return list(groups.values())


def _infer_layer(recs: List[int], events: List[Dict[str, Any]]) -> str:
    """Heuristic layer for the no-LLM path: umbrella if the dominant date cluster
    spans more than ~20 days, else instance."""
    windows = [(events[i]["start"], events[i]["end"], events[i]["precision_days"]) for i in recs]
    dom = dominant_window_cluster(windows)
    if not dom:
        return "instance"
    span = (max((e or s) for s, e, _ in dom).date() - min(s for s, _e, _ in dom).date()).days
    return "umbrella" if span > 20 else "instance"


def _unit_name(u: int, units: List[List[int]], events: List[Dict[str, Any]]) -> str:
    rep = max(units[u], key=lambda i: events[i]["n_sources"])
    return events[rep]["name"] or ""


def _peel_siblings(uid_list: List[int], units: List[List[int]], events: List[Dict[str, Any]]
                   ) -> Tuple[List[int], List[int]]:
    """Split an LLM-merged unit set into (core, peeled) where no two peeled units are
    siblings of each other or of a core unit. Each sibling-involved unit is peeled to
    its own instance; the rest stay as the merged core."""
    names = {u: _unit_name(u, units, events) for u in uid_list}
    involved: set = set()
    for x in range(len(uid_list)):
        for y in range(x + 1, len(uid_list)):
            ux, uy = uid_list[x], uid_list[y]
            if _are_siblings(names[ux], names[uy]):
                involved.add(ux)
                involved.add(uy)
    core = [u for u in uid_list if u not in involved]
    peeled = [u for u in uid_list if u in involved]
    return core, peeled


def run_pipeline(events: List[Dict[str, Any]], use_llm: bool
                 ) -> List[Tuple[List[int], str, int]]:
    """Full reconciliation pipeline over loaded `events`; returns the list of
    reconciled events as (record_idxs, layer, component_index). Pure w.r.t. kgdb
    (read-only) — the merge plan is derived from this by the caller."""
    n = len(events)
    pairs = _candidate_pairs(events)

    # Two edge sets over the same candidate pairs:
    #  broad  (recall)    = name_score (incl. containment) >= NAME_MIN -> COMPONENTS
    #  strict (confident) = base trigram >= HIGH_TRIGRAM (no containment) -> UNITS
    uf_comp = _UF(n)
    strict_adj: Dict[int, set] = defaultdict(set)
    broad_sim: Dict[Tuple[int, int], float] = {}
    rej_geo = rej_name = rej_date = 0
    for i, j in pairs:
        a, b = events[i], events[j]
        if not _geo_compatible(a, b):
            rej_geo += 1
            continue
        if _date_rejects(a, b):
            rej_date += 1
            continue
        sc = name_score(a["name"], b["name"])
        if sc < NAME_MIN:
            rej_name += 1
            continue
        uf_comp.union(i, j)
        broad_sim[(i, j)] = sc
        # Strict (unit) merge, UNLESS the two are siblings (distinct instances that
        # merely share a stem — M-17 vs M-20, varonil vs femenil): those must not
        # collapse into one unit, so the sibling guard can keep them apart.
        if name_similarity(a["name"], b["name"]) >= HIGH_TRIGRAM and not _are_siblings(a["name"], b["name"]):
            strict_adj[i].add(j)
            strict_adj[j].add(i)
    print(f"candidate pairs: {len(pairs)}  broad edges (components): {len(broad_sim)}  "
          f"(rejected geo={rej_geo} name={rej_name} date={rej_date})")

    comps: Dict[int, List[int]] = defaultdict(list)
    for idx in range(n):
        comps[uf_comp.find(idx)].append(idx)

    # Venue-purity: a component may not span two contradicting fine venues; coarse
    # records attach to their best-name-score single venue (never bridge several).
    split = 0
    final_components: List[List[int]] = []
    for comp in comps.values():
        if len(comp) < 2:
            final_components.append(comp)
            continue
        venues = {events[i]["ids"].get(7) or events[i]["ids"].get(6) for i in comp}
        venues.discard(None)
        if len(venues) <= 1:
            final_components.append(comp)
            continue
        split += 1
        by_venue: Dict[str, List[int]] = defaultdict(list)
        floating: List[int] = []
        for i in comp:
            vk = events[i]["ids"].get(7) or events[i]["ids"].get(6)
            (by_venue[vk] if vk else floating).append(i)
        for i in floating:
            best_v, best_s = None, -1.0
            for j in comp:
                vk = events[j]["ids"].get(7) or events[j]["ids"].get(6)
                s = broad_sim.get((min(i, j), max(i, j)))
                if vk and s is not None and s > best_s:
                    best_v, best_s = vk, s
            if best_v is not None:
                by_venue[best_v].append(i)
            else:
                final_components.append([i])
        final_components.extend(by_venue.values())
    if split:
        print(f"venue-purity: split {split} cross-venue component(s)")

    # Deterministic units within each final component (near-identical names merge here).
    units: List[List[int]] = []
    comp_uids: List[List[int]] = []
    for comp in final_components:
        uids = []
        for u in _local_units(comp, strict_adj):
            uids.append(len(units))
            units.append(u)
        comp_uids.append(uids)

    multi = [ci for ci, comp in enumerate(final_components)
             if len(comp) >= 2 and len(comp_uids[ci]) > 1]
    merge_comps = [ci for ci, comp in enumerate(final_components) if len(comp) >= 2]
    print(f"deterministic: {len(units)} units in {len(final_components)} components; "
          f"{len(merge_comps)} multi-record components, {len(multi)} need the LLM "
          f"(unit-cluster + layer)")

    # Adjudicate multi-unit components (batched) — else keep each unit separate.
    llm_result: Dict[int, List[Tuple[List[int], str]]] = {}
    if use_llm and multi:
        print(f"\n=== LLM: {len(multi)} multi-unit components (batched, cached) ===")
        llm_result = adjudicate_components([(comp_uids[ci], None) for ci in multi], units, events)

    # Build the final reconciled events: (record_idxs, layer, ci). The sibling guard
    # peels distinct-instance siblings (M-17 vs M-20, varonil vs femenil) the LLM merged.
    final_events: List[Tuple[List[int], str, int]] = []
    peeled_n = 0
    for ci, comp in enumerate(final_components):
        uids = comp_uids[ci]
        if ci in multi and multi.index(ci) in llm_result:
            for uid_list, layer in llm_result[multi.index(ci)]:
                core, peeled = _peel_siblings(uid_list, units, events)
                peeled_n += len(peeled)
                if core:
                    final_events.append(([r for u in core for r in units[u]], layer, ci))
                for u in peeled:  # each sibling becomes its own instance
                    final_events.append((units[u], "instance", ci))
        else:
            for u in uids:  # single unit, or multi-unit with no LLM (keep separate)
                final_events.append((units[u], _infer_layer(units[u], events), ci))
    if peeled_n:
        print(f"sibling guard: peeled {peeled_n} distinct-instance unit(s) the LLM merged")
    return final_events


def merge_plan(events: List[Dict[str, Any]],
               final_events: List[Tuple[List[int], str, int]]) -> List[Dict[str, Any]]:
    """Machine-readable plan: one entry per multi-entity reconciled event."""
    plan = []
    for recs, layer, _ci in final_events:
        if len(recs) < 2:
            continue
        plan.append({"entity_ids": sorted(events[i]["id"] for i in recs),
                     "layer": layer,
                     "n_sources": sum(events[i]["n_sources"] for i in recs)})
    return plan


def main() -> None:
    supertype = os.environ.get("RECON_SUPERTYPE")
    conn = _connect()
    try:
        events = _load_events(conn, supertype)
    finally:
        conn.close()
    print(f"loaded {len(events)} canonical events"
          + (f" (supertype={supertype})" if supertype else " (all supertypes)"))

    final_events = run_pipeline(events, USE_LLM)

    merges = [ev for ev in final_events if len(ev[0]) > 1]
    collapsed = sum(len(recs) for recs, _l, _ci in merges)
    print(f"\nreconciled events: {len(final_events)} total; "
          f"{len(merges)} multi-record events collapse {collapsed} canonicals "
          f"(net −{collapsed - len(merges)})")

    for recs, layer, ci in sorted(final_events, key=lambda ev: -len(ev[0])):
        if len(recs) < 2:
            continue
        agg = aggregate_event(recs, layer, events)
        st = events[recs[0]]["supertype"]
        d = agg["date"]
        drange = f"{d['start']}..{d['end']}" + (f" pd={d['precision_days']}"
                                                if d.get("precision_days") is not None else "")
        loc = agg["location"].get("venue") or ",".join(
            v[-7:] for v in (agg["location"].get("venues") or []))
        out = f" drop={agg['outliers_dropped']}" if agg["outliers_dropped"] else ""
        print(f"\n=== [{layer.upper()}] {len(recs)} recs / {agg['n_sources']} src  "
              f"{st}  {drange}  loc={loc}{out}")
        for i in sorted(recs, key=lambda i: -events[i]["n_sources"]):
            e = events[i]
            dd = e["start"].date().isoformat() if e["start"] else "?"
            print(f"    id={e['id']:<6} src={e['n_sources']:>3} {e['event_type'] or '?':<14} "
                  f"{dd} (pd={e['precision_days']})  {e['name'][:56]!r}")


if __name__ == "__main__":
    main()
