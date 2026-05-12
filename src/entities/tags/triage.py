"""Type-triage step (design §5.2).

Catalog-free classifier: each item gets zero or more `TypeTriageItem` rows,
one per distinct stance idea (post tie-break). The downstream `StanceTagger`
consumes these as per-type candidate lists.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.entities.tags.llm import JsonLlm
from src.entities.tags.models import (
    Customer,
    LinkedEventContext,
    SourceItem,
    StanceType,
    TypeTriageItem,
    TypeTriageResult,
)
from src.entities.tags.prompts import triage_prompt


logger = logging.getLogger(__name__)


VALID_STANCE_TYPES: frozenset[str] = frozenset(
    {
        "entity_stance",
        "complaint",
        "gratefulness",
        "suggestion",
        "request",
        "denuncia",
        "question",
        "endorsement",
        # `noise` is intentionally absent: the triage prompt asks the model
        # to OMIT noise items entirely. If the model emits one anyway, we
        # drop the row silently (counted as `dropped_invalid_type`).
    }
)
VALID_IMPORTANCE_HINTS: frozenset[str] = frozenset({"low", "medium", "high"})

DEFAULT_MAX_ROWS_PER_ITEM = 4
DEFAULT_HINT_TEXT_LIMIT = 800


class TypeTriageStep:
    """Triage items into typed stance ideas (one row per idea)."""

    def __init__(
        self,
        customer: Customer,
        llm: JsonLlm,
        *,
        max_rows_per_item: int = DEFAULT_MAX_ROWS_PER_ITEM,
        hint_text_limit: int = DEFAULT_HINT_TEXT_LIMIT,
    ):
        self.customer = customer
        self.llm = llm
        self.max_rows_per_item = max_rows_per_item
        self.hint_text_limit = hint_text_limit

    def triage(
        self,
        items: list[SourceItem],
        event: Optional[LinkedEventContext] = None,
    ) -> TypeTriageResult:
        result = TypeTriageResult(triaged=[], n_items_seen=len(items))
        if not items:
            return result

        id_map: dict[int, str] = {}
        prompt = triage_prompt(self.customer, items, event, id_map=id_map)
        kind_by_id = {item.id: item.kind for item in items}
        text_by_id = {item.id: item.short_text(self.hint_text_limit) for item in items}

        response = self.llm.call(prompt)
        rows = None
        if isinstance(response, dict):
            rows = response.get("rows", response.get("triage"))
        if not isinstance(rows, list):
            logger.warning("triage: malformed response (no `rows` list)")
            return result

        per_item_rows: dict[str, list[TypeTriageItem]] = {}
        dropped_unknown = 0
        dropped_invalid_type = 0
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            local_id = raw.get("id", raw.get("source_item_id"))
            try:
                local_id_int = int(local_id)
            except (TypeError, ValueError):
                dropped_unknown += 1
                continue
            canonical = id_map.get(local_id_int)
            if canonical is None:
                dropped_unknown += 1
                continue
            stype = raw.get("type", raw.get("stance_type"))
            if stype not in VALID_STANCE_TYPES:
                dropped_invalid_type += 1
                continue
            hint = raw.get("importance", raw.get("importance_hint"))
            if hint is not None and hint not in VALID_IMPORTANCE_HINTS:
                hint = None
            summary = raw.get("summary", raw.get("brief_summary"))
            row = TypeTriageItem(
                source_item_id=canonical,
                source_kind=kind_by_id.get(canonical, "user_comment"),  # type: ignore[arg-type]
                stance_type=stype,  # type: ignore[arg-type]
                brief_summary=str(summary or ""),
                importance_hint=hint,  # type: ignore[arg-type]
                text=text_by_id.get(canonical, ""),
            )
            per_item_rows.setdefault(canonical, []).append(row)

        # Apply per-item rules: noise is exclusive; soft cap.
        for canonical, rows_for_item in per_item_rows.items():
            noise_rows = [r for r in rows_for_item if r.stance_type == "noise"]
            if noise_rows:
                # Exactly one noise row, no other stance.
                result.triaged.append(noise_rows[0])
                continue
            # Soft cap.
            kept = rows_for_item[: self.max_rows_per_item]
            result.triaged.extend(kept)

        if dropped_unknown or dropped_invalid_type:
            logger.debug(
                "triage: dropped %d unknown ids, %d invalid stance_type rows",
                dropped_unknown,
                dropped_invalid_type,
            )
        return result

    def group_by_type(
        self, triage: TypeTriageResult
    ) -> dict[StanceType, list[TypeTriageItem]]:
        out: dict[StanceType, list[TypeTriageItem]] = {}
        for row in triage.triaged:
            out.setdefault(row.stance_type, []).append(row)
        return out
