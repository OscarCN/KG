"""Adapters around upstream entity extraction output.

`tags_gpt` does not reimplement `src.entities.extraction`. This module
turns already-extracted records into streamable source batches so tests
and runners can emulate the production streaming shape.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from src.entities.tags_gpt.models import SourceBatch


def load_extracted_records(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON list of extracted records in {path}")
    return [dict(x) for x in data]


def group_by_source(records: Iterable[dict]) -> list[SourceBatch]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        source_id = str(record.get("_source_id") or "")
        grouped[source_id].append(dict(record))
    return [SourceBatch(source_id=source_id, extracted_records=items) for source_id, items in grouped.items()]


def sort_batches_by_publication(batches: Iterable[SourceBatch]) -> list[SourceBatch]:
    return sorted(
        batches,
        key=lambda batch: min(
            (record.get("date_created") or record.get("publication_date") or "")
            for record in batch.extracted_records
        ),
    )
