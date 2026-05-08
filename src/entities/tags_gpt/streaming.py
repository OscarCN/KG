"""Small streaming coordinator for the decoupled steps."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.entities.tags_gpt.catalogs import ClaimCatalogStore, EventStore, StanceCatalog
from src.entities.tags_gpt.linking import EventLinkingStep
from src.entities.tags_gpt.models import (
    ArticleBundle,
    ArticleProcessResult,
    EventTagResult,
    SourceBatch,
    SourceItem,
    StepSummary,
)
from src.entities.tags_gpt.retrieval import ContentRetriever
from src.entities.tags_gpt.tagging import ClaimTagger, ClaimUpdater, StanceTagger, StanceUpdater


@dataclass
class StreamingState:
    event_store: EventStore
    stance_catalog: StanceCatalog
    claim_catalogs: ClaimCatalogStore = field(default_factory=ClaimCatalogStore)
    items_seen: dict[str, SourceItem] = field(default_factory=dict)


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
        linker: EventLinkingStep,
        stance_tagger: StanceTagger,
        stance_updater: StanceUpdater,
        claim_tagger: ClaimTagger,
        claim_updater: ClaimUpdater,
    ):
        self.state = state
        self.retriever = retriever
        self.linker = linker
        self.stance_tagger = stance_tagger
        self.stance_updater = stance_updater
        self.claim_tagger = claim_tagger
        self.claim_updater = claim_updater

    def process_batch(self, batch: SourceBatch) -> ArticleProcessResult:
        bundle = self.retriever.get_article_bundle(batch.source_id)
        result = ArticleProcessResult(source_id=batch.source_id)

        result.summaries.append(self._remember_items(bundle))
        result.link_results = [self.linker.link_record(record) for record in batch.extracted_records]

        for event_id, event in self._unique_linked_events(result):
            stance_tagging = self.stance_tagger.tag(event, bundle.items, self.state.stance_catalog)
            stance_summary = self.stance_updater.update(
                self.state.stance_catalog,
                stance_tagging,
                sample_items=bundle.items,
            )
            claim_tagging = self.claim_tagger.tag(event, bundle.items)
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
