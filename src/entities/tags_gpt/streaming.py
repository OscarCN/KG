"""Streaming coordinator for ArticleBundle processing."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.entities.tags_gpt.catalogs import ClaimCatalogStore, StanceCatalog
from src.entities.tags_gpt.models import (
    ArticleBundle,
    ArticleProcessResult,
    ClaimTagging,
    EventTagResult,
    STANCE_BEARING_TYPES,
    TAG_ONLY_TYPES,
    StanceAssignment,
    StanceTagging,
    StepSummary,
    TypeTriageItem,
)
from src.entities.tags_gpt.tagging import (
    ClaimTagger,
    ClaimUpdater,
    StanceTagger,
    StanceUpdater,
    TypeTriageStep,
)


@dataclass
class StreamingState:
    stance_catalog: StanceCatalog
    claim_catalogs: ClaimCatalogStore = field(default_factory=ClaimCatalogStore)


class StreamingTagsPipeline:
    def __init__(
        self,
        *,
        state: StreamingState,
        type_triage: TypeTriageStep,
        stance_tagger: StanceTagger,
        stance_updater: StanceUpdater,
        claim_tagger: ClaimTagger,
        claim_updater: ClaimUpdater,
        triage_batch_size: int = 15,
    ):
        self.state = state
        self.type_triage = type_triage
        self.stance_tagger = stance_tagger
        self.stance_updater = stance_updater
        self.claim_tagger = claim_tagger
        self.claim_updater = claim_updater
        self.triage_batch_size = max(1, triage_batch_size)

    def process_bundle(self, bundle: ArticleBundle) -> ArticleProcessResult:
        result = ArticleProcessResult(source_id=bundle.source_id)
        remember = StepSummary("retrieve_content")
        remember.inc(bundle.root.kind)
        remember.inc("user_comment", len(bundle.comments))
        result.summaries.append(remember)

        triage = self.type_triage.triage(bundle, event=bundle.linked_events[0] if bundle.linked_events else None, batch_size=self.triage_batch_size)
        triage_summary = StepSummary("type_triage")
        triage_summary.inc("items_seen", triage.n_items_seen)
        triage_summary.inc("ideas", len(triage.triaged))
        triage_summary.inc("dropped_invalid", triage.dropped_invalid)
        result.summaries.append(triage_summary)

        stance_tagging = self._noise_assignments(bundle, triage.triaged)
        for stance_type in STANCE_BEARING_TYPES:
            hints = [hint for hint in triage.triaged if hint.stance_type == stance_type]
            if not hints:
                continue
            tagged = self.stance_tagger.tag(
                event=bundle.linked_events[0] if bundle.linked_events else None,
                items=bundle.items,
                catalog=self.state.stance_catalog,
                stance_type=stance_type,
                triage_hints=hints,
            )
            stance_tagging.assignments.extend(tagged.assignments)
            stance_tagging.proposals.extend(tagged.proposals)
            stance_tagging.dropped_assignments += tagged.dropped_assignments
            for key, value in tagged.n_assignments_by_type.items():
                stance_tagging.n_assignments_by_type[key] = stance_tagging.n_assignments_by_type.get(key, 0) + value
        stance_update = self.stance_updater.update(self.state.stance_catalog, stance_tagging)

        if not bundle.linked_events:
            result.event_tag_results.append(
                EventTagResult(
                    event_id="",
                    stance_tagging=stance_tagging,
                    stance_update=stance_update,
                    claim_tagging=ClaimTagging(),
                    claim_update=StepSummary("claim_update"),
                )
            )
            return result

        for event in bundle.linked_events:
            claim_tagging = self.claim_tagger.tag(
                bundle=bundle,
                event=event,
                claim_catalogs=self.state.claim_catalogs,
            )
            claim_update = self.claim_updater.update(
                claim_catalogs=self.state.claim_catalogs,
                event=event,
                tagging=claim_tagging,
            )
            result.event_tag_results.append(
                EventTagResult(
                    event_id=event.id,
                    stance_tagging=stance_tagging,
                    stance_update=stance_update,
                    claim_tagging=claim_tagging,
                    claim_update=claim_update,
                )
            )
        return result

    def _noise_assignments(self, bundle: ArticleBundle, hints: list[TypeTriageItem]) -> StanceTagging:
        tagging = StanceTagging()
        for hint in hints:
            if hint.stance_type not in TAG_ONLY_TYPES:
                continue
            tagging.assignments.append(
                StanceAssignment(
                    source_item_id=hint.source_item_id,
                    source_kind=hint.source_kind,
                    customer_id=bundle.customer.entity_id if bundle.customer else self.state.stance_catalog.customer_id,
                    stance_id=None,
                    stance_type=hint.stance_type,
                    event_id=bundle.event_ids[0] if bundle.event_ids else None,
                    reason=hint.brief_summary or "type_triage",
                )
            )
            tagging.n_assignments_by_type[hint.stance_type] = tagging.n_assignments_by_type.get(hint.stance_type, 0) + 1
        return tagging
