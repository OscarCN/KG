"""Streaming pipeline driving one `ArticleBundle` at a time (design §4).

`StreamingTagsPipeline.process_bundle(bundle)` runs:
    triage → per-active-type stance tag → stance update
    [if event_ids: per (root, event_id) claim extract → claim update]
    item-counter increments, consistency-pass dispatch (deferred to runner).
"""

from __future__ import annotations

import logging
from typing import Optional

from src.entities.tags.catalogs import ClaimCatalog, ClaimCatalogStore, StanceCatalog
from src.entities.tags.models import (
    ArticleBundle,
    ArticleProcessResult,
    Customer,
    EventTagResult,
    SourceItem,
    StanceTagging,
    StepSummary,
    StanceType,
    TypeTriageResult,
)
from src.entities.tags.tagging import (
    ClaimTagger,
    ClaimUpdater,
    StanceTagger,
    StanceUpdater,
)
from src.entities.tags.triage import TypeTriageStep


logger = logging.getLogger(__name__)


STANCE_BEARING_ACTIVE_TYPES: tuple[StanceType, ...] = (
    "entity_stance",
    "complaint",
    "gratefulness",
    "suggestion",
    "request",
    "denuncia",
    "question",
    "endorsement",
)


class StreamingState:
    """Mutable in-memory state for the streaming pipeline."""

    def __init__(
        self,
        customer: Customer,
        stance_catalog: StanceCatalog,
        claim_catalogs: Optional[ClaimCatalogStore] = None,
    ):
        self.customer = customer
        self.stance_catalog = stance_catalog
        self.claim_catalogs = claim_catalogs or ClaimCatalogStore()
        self.items_seen: dict[str, SourceItem] = {}


class StreamingTagsPipeline:
    """Composes the steps with explicit boundaries.

    Each step is injectable so tests / future remoting can swap in fakes.
    """

    def __init__(
        self,
        *,
        state: StreamingState,
        triage_step: TypeTriageStep,
        stance_tagger: StanceTagger,
        stance_updater: StanceUpdater,
        claim_tagger: ClaimTagger,
        claim_updater: ClaimUpdater,
    ):
        self.state = state
        self.triage_step = triage_step
        self.stance_tagger = stance_tagger
        self.stance_updater = stance_updater
        self.claim_tagger = claim_tagger
        self.claim_updater = claim_updater

    def process_bundle(self, bundle: ArticleBundle) -> ArticleProcessResult:
        result = ArticleProcessResult(source_id=bundle.root.id)

        # 1. Remember items
        remember_summary = self._remember_items(bundle)
        result.summaries.append(remember_summary)

        # 2. Triage
        items = bundle.all_items
        primary_event = bundle.linked_events[0] if bundle.linked_events else None
        triage = self.triage_step.triage(items, event=primary_event)
        result.summaries.append(self._triage_summary(triage))

        # 3. Stance tagging — one call per active stance-bearing type.
        # (Noise items are omitted by the triage prompt itself, so there's
        # no tag-only emission step.)
        per_type = self.triage_step.group_by_type(triage)
        merged_stance = StanceTagging()
        merged_update_summary = StepSummary(name="stance_update_merged")
        for stance_type in STANCE_BEARING_ACTIVE_TYPES:
            hints = per_type.get(stance_type) or []
            if not hints:
                continue
            tagging = self.stance_tagger.tag(
                self.state.stance_catalog,
                stance_type=stance_type,
                items=items,
                triage_hints=hints,
                event=primary_event,
            )
            update_summary = self.stance_updater.update(self.state.stance_catalog, tagging)
            self._merge_stance_tagging(merged_stance, tagging)
            for k, v in update_summary.counters.items():
                merged_update_summary.inc(k, v)

        # 4. Claims — only when bundle has linked events.
        event_results: list[EventTagResult] = []
        if bundle.event_ids and bundle.linked_events:
            for event in bundle.linked_events:
                catalog = self.state.claim_catalogs.get_or_create(
                    self.state.customer.entity_id, event.id
                )
                claim_tagging = self.claim_tagger.tag(
                    event,
                    bundle.root,
                    bundle.comments,
                    list(catalog.clusters.values()),
                )
                claim_update = self.claim_updater.update(catalog, event, claim_tagging.claims)
                event_results.append(
                    EventTagResult(
                        event_id=event.id,
                        stance_tagging=None,
                        stance_update=None,
                        claim_tagging=claim_tagging,
                        claim_update=claim_update,
                    )
                )

        # Attach the merged stance result to a synthetic per-bundle entry
        # for visibility (the stance pipeline is event-independent in scope,
        # but we still want to surface its counters in the result).
        if merged_stance.assignments or merged_stance.proposals:
            event_results.insert(
                0,
                EventTagResult(
                    event_id="__bundle__",
                    stance_tagging=merged_stance,
                    stance_update=merged_update_summary,
                ),
            )
        result.event_tag_results = event_results

        # 5. Counters — one increment per bundle. There is no separate
        # "items" counter because every item in a bundle is processed
        # in this same call; the counter tracks `process_bundle` calls,
        # which is what `consistency_pass_due` checks against.
        self.state.customer.bundles_processed_total += 1
        self.state.customer.bundles_processed_since_last_pass += 1
        return result

    # ── helpers ────────────────────────────────────────────────────────

    def _remember_items(self, bundle: ArticleBundle) -> StepSummary:
        summary = StepSummary(name="remember_items")
        for item in bundle.all_items:
            self.state.items_seen[item.id] = item
            summary.inc(item.kind)
        summary.inc("event_ids", len(bundle.event_ids))
        return summary

    @staticmethod
    def _triage_summary(triage: TypeTriageResult) -> StepSummary:
        summary = StepSummary(name="triage")
        summary.inc("items_seen", triage.n_items_seen)
        summary.inc("rows", len(triage.triaged))
        for row in triage.triaged:
            summary.inc(f"type_{row.stance_type}")
        return summary

    @staticmethod
    def _merge_stance_tagging(target: StanceTagging, source: StanceTagging) -> None:
        target.assignments.extend(source.assignments)
        target.proposals.extend(source.proposals)
        target.dropped_assignments += source.dropped_assignments
        target.n_items_tagged_no_stance += source.n_items_tagged_no_stance
        for k, v in source.n_assignments_by_type.items():
            target.n_assignments_by_type[k] = target.n_assignments_by_type.get(k, 0) + v
