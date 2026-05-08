"""Decoupled streaming tags pipeline.

`src.entities.tags_gpt` is an alternate implementation of the tags flow
focused on readability and testability. It keeps every phase callable in
isolation:

1. upstream extraction adapter,
2. content retrieval,
3. event candidate retrieval,
4. event linking,
5. stance tagging,
6. stance updating,
7. claim tagging,
8. claim updating.
"""

from src.entities.tags_gpt.bootstrap import StanceBootstrapStep
from src.entities.tags_gpt.catalogs import ClaimCatalog, ClaimCatalogStore, EventStore, StanceCatalog
from src.entities.tags_gpt.extraction import group_by_source, load_extracted_records, sort_batches_by_publication
from src.entities.tags_gpt.linking import (
    EventLinkingStep,
    ExactTitleDecider,
    LlmLinkDecider,
    NoMatchDecider,
)
from src.entities.tags_gpt.llm import CachedJsonLlm, OpenRouterJsonLlm, ScriptedJsonLlm, default_cached_llm
from src.entities.tags_gpt.models import (
    ArticleBundle,
    ArticleProcessResult,
    ClaimTagging,
    ContentGraph,
    Customer,
    EventMention,
    EventTagResult,
    LinkedEvent,
    LinkResult,
    RawClaim,
    SourceBatch,
    SourceItem,
    StanceAssignment,
    StanceEntry,
    StanceProposal,
    StanceTagging,
    StepSummary,
)
from src.entities.tags_gpt.persistence import load_content_graph, save_snapshot
from src.entities.tags_gpt.retrieval import ContentRetriever, EsNewsRetriever, LocalJsonRetriever
from src.entities.tags_gpt.streaming import StreamingState, StreamingTagsPipeline
from src.entities.tags_gpt.tagging import ClaimTagger, ClaimUpdater, StanceTagger, StanceUpdater

__all__ = [
    "ArticleBundle",
    "ArticleProcessResult",
    "CachedJsonLlm",
    "ClaimCatalog",
    "ClaimCatalogStore",
    "ClaimTagger",
    "ClaimTagging",
    "ClaimUpdater",
    "ContentGraph",
    "ContentRetriever",
    "Customer",
    "default_cached_llm",
    "EsNewsRetriever",
    "EventLinkingStep",
    "EventMention",
    "EventStore",
    "EventTagResult",
    "ExactTitleDecider",
    "group_by_source",
    "LinkedEvent",
    "LinkResult",
    "LlmLinkDecider",
    "load_content_graph",
    "load_extracted_records",
    "LocalJsonRetriever",
    "NoMatchDecider",
    "OpenRouterJsonLlm",
    "RawClaim",
    "save_snapshot",
    "ScriptedJsonLlm",
    "sort_batches_by_publication",
    "SourceBatch",
    "SourceItem",
    "StanceAssignment",
    "StanceBootstrapStep",
    "StanceCatalog",
    "StanceEntry",
    "StanceProposal",
    "StanceTagger",
    "StanceTagging",
    "StanceUpdater",
    "StepSummary",
    "StreamingState",
    "StreamingTagsPipeline",
]
