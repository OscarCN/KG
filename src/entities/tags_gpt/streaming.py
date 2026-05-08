"""Small streaming coordinator for the decoupled steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from src.entities.tags_gpt.catalogs import ClaimCatalogStore, EventStore, StanceCatalog
from src.entities.tags_gpt.models import (
    ArticleBundle,
    ArticleProcessResult,
    ClaimTagging,
    EventTagResult,
    STANCE_BEARING_TYPES,
    STANCE_TYPES,
    SourceBatch,
    SourceItem,
    StanceAssignment,
    StanceTagging,
    StepSummary,
    TypeTriageItem,
    TypeTriageResult,
)
from src.entities.tags_gpt.retrieval import ContentRetriever
from src.entities.tags_gpt.tagging import (
    ClaimTagger,
    ClaimUpdater,
    StanceTagger,
    StanceUpdater,
    TypeTriageStep,
)


class LinkerStep(Protocol):
    def link_record(self, record: dict): ...


@dataclass
class StreamingState:
    event_store: EventStore
    stance_catalog: StanceCatalog
    claim_catalogs: ClaimCatalogStore = field(default_factory=ClaimCatalogStore)
    items_seen: dict[str, SourceItem] = field(default_factory=dict)
    tagging_strategy: Literal["single_pass", "two_pass"] = "two_pass"


class StreamingTagsPipeline:
    """Calls each step in order without hiding the boundaries.

    A caller can replace any step with a fake or a remote-service client:
    retrieval, linking, stance tagging, stance updating, claim tagging, or
    claim updating.
    """

    def __init__(
        self,
        *,
        state: StreamingState,
        retriever: ContentRetriever,
        linker: LinkerStep,
        stance_tagger: StanceTagger,
        stance_updater: StanceUpdater,
        claim_tagger: ClaimTagger,
        claim_updater: ClaimUpdater,
        type_triage_step: TypeTriageStep | None = None,
    ):
        self.state = state
        self.retriever = retriever
        self.linker = linker
        self.stance_tagger = stance_tagger
        self.stance_updater = stance_updater
        self.claim_tagger = claim_tagger
        self.claim_updater = claim_updater
        self.type_triage_step = type_triage_step

    def process_batch(self, batch: SourceBatch) -> ArticleProcessResult:
        bundle = self.retriever.get_article_bundle(batch.source_id)
        result = ArticleProcessResult(source_id=batch.source_id)

        result.summaries.append(self._remember_items(bundle))
        result.link_results = [self.linker.link_record(record) for record in batch.extracted_records]

        for event_id, event in self._unique_linked_events(result):
            if self.state.tagging_strategy == "two_pass":
                stance_tagging, claim_tagging, triage_summary = self._tag_two_pass(event, bundle.items)
                result.summaries.append(triage_summary)
            else:
                stance_tagging = self.stance_tagger.tag(event, bundle.items, self.state.stance_catalog)
                claim_items = [item for item in bundle.items if item.kind in self.claim_tagger.source_kinds]
                claim_tagging = self.claim_tagger.tag(event, claim_items)
            stance_summary = self.stance_updater.update(
                self.state.stance_catalog,
                stance_tagging,
                sample_items=bundle.items,
            )
            claim_summary = self.claim_updater.update(
                self.state.claim_catalogs,
                event,
                claim_tagging,
            )
            result.event_tag_results.append(
                EventTagResult(
                    event_id=event_id,
                    stance_tagging=stance_tagging,
                    stance_update=stance_summary,
                    claim_tagging=claim_tagging,
                    claim_update=claim_summary,
                )
            )

        return result

    def _tag_two_pass(self, event, items: list[SourceItem]) -> tuple[StanceTagging, ClaimTagging, StepSummary]:
        if self.type_triage_step is None:
            raise ValueError("two_pass tagging requires a TypeTriageStep")
        triage = self.type_triage_step.triage(event, items)
        summary = self._triage_summary(triage)
        stance_tagging = self._tag_only_assignments(event.id, triage)

        hints_by_type: dict[str, list[TypeTriageItem]] = {}
        for hint in triage.triaged:
            if hint.stance_type in STANCE_BEARING_TYPES:
                hints_by_type.setdefault(hint.stance_type, []).append(hint)

        for stance_type in STANCE_TYPES:
            if stance_type not in hints_by_type:
                continue
            typed = self.stance_tagger.tag(
                event,
                items,
                self.state.stance_catalog,
                stance_type=stance_type,
                triage_hints=hints_by_type[stance_type],
            )
            self._extend_stance_tagging(stance_tagging, typed)

        claim_tagging = ClaimTagging(
            claims=list(triage.claims),
            dropped_invalid=triage.dropped_claim_invalid,
            dropped_off_customer=triage.dropped_off_customer,
        )
        return stance_tagging, claim_tagging, summary

    def _tag_only_assignments(self, event_id: str, triage: TypeTriageResult) -> StanceTagging:
        result = StanceTagging()
        for hint in triage.triaged:
            if hint.stance_type in STANCE_BEARING_TYPES:
                continue
            result.assignments.append(
                StanceAssignment(
                    source_item_id=hint.source_item_id,
                    source_kind=hint.source_kind,
                    customer_id=self.state.stance_catalog.customer_id,
                    stance_id=None,
                    stance_type=hint.stance_type,
                    sentiment=hint.sentiment,
                    consistency_relevance=hint.importance_hint,
                    event_id=event_id,
                    reason=hint.brief_summary,
                )
            )
            result.n_assignments_by_type[hint.stance_type] = (
                result.n_assignments_by_type.get(hint.stance_type, 0) + 1
            )
        result.n_items_tagged_no_stance = len({x.source_item_id for x in result.assignments})
        return result

    @staticmethod
    def _extend_stance_tagging(target: StanceTagging, source: StanceTagging) -> None:
        target.assignments.extend(source.assignments)
        target.proposals.extend(source.proposals)
        target.dropped_assignments += source.dropped_assignments
        target.n_items_tagged_no_stance += source.n_items_tagged_no_stance
        for stance_type, count in source.n_assignments_by_type.items():
            target.n_assignments_by_type[stance_type] = (
                target.n_assignments_by_type.get(stance_type, 0) + count
            )

    @staticmethod
    def _triage_summary(triage: TypeTriageResult) -> StepSummary:
        summary = StepSummary("type_triage")
        summary.inc("items_seen", triage.n_items_seen)
        summary.inc("ideas", len(triage.triaged))
        summary.inc("claims", len(triage.claims))
        summary.inc("dropped_invalid", triage.dropped_invalid)
        summary.inc("dropped_off_customer", triage.dropped_off_customer)
        for hint in triage.triaged:
            summary.inc(f"type_{hint.stance_type}")
        return summary

    def _remember_items(self, bundle: ArticleBundle) -> StepSummary:
        summary = StepSummary("retrieve_content")
        for item in bundle.items:
            self.state.items_seen[item.id] = item
            summary.inc(item.kind)
        return summary

    @staticmethod
    def _unique_linked_events(result: ArticleProcessResult):
        seen: set[str] = set()
        for link_result in result.link_results:
            if link_result.status not in ("created", "merged") or not link_result.event:
                continue
            if link_result.event.id in seen:
                continue
            seen.add(link_result.event.id)
            yield link_result.event.id, link_result.event
