"""
Create an incoming-document fixture from one row of keywords.xlsx.

Each row in ``src/entities/extraction/catalogues/keywords.xlsx`` represents
one ontology matching rule. This script translates a selected row into an
Elasticsearch request and saves the matching documents under ``data/`` so
``src/entities/run_entities.py`` can simulate a production content stream.

Examples:
    python src/PoC/get_entities_data.py
    GET_ENTITIES_ROW_INDEX=31 python src/PoC/get_entities_data.py
    GET_ENTITIES_CLASS=emergency_general GET_ENTITIES_LIMIT=100 python src/PoC/get_entities_data.py

Selection env vars:
    GET_ENTITIES_ROW_INDEX  zero-based DataFrame row index from keywords.xlsx
    GET_ENTITIES_CLASS      first enabled row whose "class" matches this value
    GET_ENTITIES_TAG        optional extra filter when selecting by class

Query env vars:
    GET_ENTITIES_PERIOD         overrides row period; default "w"
    GET_ENTITIES_LIMIT          max ES hits
    GET_ENTITIES_CVEGEO         comma-separated location ids, e.g. "22014"
    GET_ENTITIES_LOCATION_TYPE  "mentioned" (default) or "author"
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from dotenv import load_dotenv

_PROJECT_ROOT = Path("/Users/oscarcuellar/ocn/media/kg/kg/")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

load_dotenv(_PROJECT_ROOT / ".env.local")

from src.PoC.get_data import NEWS_FIELDS, fetch_docs, save_docs


KEYWORDS_PATH = (
    _PROJECT_ROOT / "src" / "entities" / "extraction" / "catalogues" / "keywords.xlsx"
)


def _is_blank(value: Any) -> bool:
    return pd.isna(value) or not str(value).strip()


def _parse_quoted_list(value: Any) -> list[str]:
    if _is_blank(value):
        return []
    raw = str(value)
    items = re.findall(r'"([^"]+)"', raw)
    if not items:
        items = [item.strip() for item in raw.split(",") if item.strip()]
    return items


def _parse_comma_list(value: Any) -> list[str]:
    if _is_blank(value):
        return []
    return [item.strip().lower() for item in str(value).split(",") if item.strip()]


def _parse_pipe_list(value: Any) -> list[str]:
    if _is_blank(value):
        return []
    return [item.strip() for item in str(value).split("|") if item.strip()]


def _categories_request(categories: list[str]) -> dict[str, list[str]]:
    """Convert category paths like ``A>B>C`` into elastic_client levels."""
    out: dict[str, list[str]] = {}
    for category in categories:
        parts = [part.strip() for part in category.split(">") if part.strip()]
        for idx, part in enumerate(parts[:5], start=1):
            out.setdefault(f"level_{idx}", [])
            if part not in out[f"level_{idx}"]:
                out[f"level_{idx}"].append(part)
    return out


def _parse_bbox(value: Any) -> list:
    if _is_blank(value):
        return []
    raw = str(value).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", raw)]
        if len(nums) == 4:
            min_lon, min_lat, max_lon, max_lat = nums
            return [[min_lon, min_lat], [max_lon, max_lat]]
    return []


def load_keywords() -> pd.DataFrame:
    df = pd.read_excel(KEYWORDS_PATH)
    if "enabled" in df.columns:
        enabled = df["enabled"].isna() | df["enabled"].astype(bool)
        df = df[enabled]
    return df


def select_row(df: pd.DataFrame) -> tuple[int, pd.Series]:
    row_index = os.environ.get("GET_ENTITIES_ROW_INDEX")
    if row_index:
        idx = int(row_index)
        if idx not in df.index:
            raise SystemExit(f"GET_ENTITIES_ROW_INDEX={idx} is not an enabled row index")
        return idx, df.loc[idx]

    class_name = os.environ.get("GET_ENTITIES_CLASS")
    if class_name:
        candidates = df[df["class"].astype(str) == class_name]
        tag = os.environ.get("GET_ENTITIES_TAG")
        if tag:
            candidates = candidates[candidates["tag"].astype(str) == tag]
        if candidates.empty:
            raise SystemExit(f"No enabled keywords.xlsx row found for class={class_name!r}")
        idx = int(candidates.index[0])
        return idx, candidates.iloc[0]

    idx = int(df.index[0])
    return idx, df.iloc[0]


def row_to_request(row: pd.Series) -> dict[str, Any]:
    doctypes = _parse_comma_list(row.get("document_type")) or ["news"]
    doctype = os.environ.get("GET_ENTITIES_DOCTYPE") or doctypes[0]
    period = (
        os.environ.get("GET_ENTITIES_PERIOD")
        or (None if _is_blank(row.get("period")) else str(row.get("period")).strip())
        or "w"
    )

    keywords = _parse_quoted_list(row.get("kw"))
    phrases = _parse_quoted_list(row.get("phrase"))
    negative = _parse_quoted_list(row.get("not"))
    if negative:
        positive = {"OR": keywords} if len(keywords) > 1 else keywords
        keywords = (
            {"AND": [positive, {"NOT": {"OR": negative}}]}
            if positive
            else {"NOT": {"OR": negative}}
        )

    request: dict[str, Any] = {
        "doctype": doctype,
        "period": period,
        "keywords": {"OR": keywords} if isinstance(keywords, list) and len(keywords) > 1 else keywords,
        "phrases": {"OR": phrases} if len(phrases) > 1 else phrases,
        "sort": "date_created",
        "page_size": int(os.environ.get("GET_ENTITIES_LIMIT") or 1000),
    }

    categories = _categories_request(_parse_pipe_list(row.get("categories")))
    if categories:
        request["categories"] = categories

    bbox = _parse_bbox(row.get("bbox"))
    if bbox:
        request["bounding_box"] = bbox
        request["geo_filter"] = True

    cvegeo_env = os.environ.get("GET_ENTITIES_CVEGEO")
    if cvegeo_env:
        request["cvegeo"] = [item.strip() for item in cvegeo_env.split(",") if item.strip()]

    if request.get("cvegeo") or request.get("bounding_box"):
        request["location_type"] = os.environ.get("GET_ENTITIES_LOCATION_TYPE", "mentioned")

    return request


def fixture_slug(row_index: int, row: pd.Series) -> str:
    label = str(row.get("tag") or row.get("class") or f"row_{row_index}")
    label = re.sub(r"[^a-zA-Z0-9]+", "_", label).strip("_").lower()
    return f"entities_{row_index}_{label}"


if __name__ == "__main__":
    df = load_keywords()
    row_index, row = select_row(df)
    request = row_to_request(row)
    limit_env = os.environ.get("GET_ENTITIES_LIMIT")
    limit: Optional[int] = int(limit_env) if limit_env else None

    print(f"keywords.xlsx row: {row_index}")
    print(f"  class={row.get('class')} tag={row.get('tag')}")
    print("ES request:")
    print(json.dumps(request, ensure_ascii=False, indent=2, default=str))

    docs = fetch_docs(request, fields=NEWS_FIELDS, limit=limit)
    slug = os.environ.get("GET_ENTITIES_OUTPUT") or fixture_slug(row_index, row)
    out_dir = _PROJECT_ROOT / "data" / slug
    save_docs(docs, out_dir, filename_stem=slug)
