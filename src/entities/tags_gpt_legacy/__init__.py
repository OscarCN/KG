"""Decoupled streaming tags pipeline.

`src.entities.tags_gpt` is an alternate implementation of the tags flow
focused on readability and testability. It keeps every phase callable in
isolation:

1. upstream extraction adapter,
2. content retrieval,
3. generalized linking via `src.entities.linking_gpt`,
4. stance tagging,
5. stance updating,
6. claim tagging,
7. claim updating.
"""

from src.entities.tags_gpt.bootstrap import StanceBootstrapStep
from src.entities.tags_gpt.catalogs import ClaimCatalog, ClaimCatalogStore, EventStore, StanceCatalog
from src.entities.tags_gpt.consistency import ConsistencyPassStep
from src.entities.tags_gpt.extraction import group_by_source, load_extracted_records, sort_batches_by_publication
from src.entities.tags_gpt.llm import CachedJsonLlm, OpenRouterJsonLlm, ScriptedJsonLlm, default_cached_llm
from src.entities.tags_gpt.models import (
    ArticleBundle,
    ArticleProcessResult,
    ClaimTagging,
    ConsistencyPassResult,
    ContentGraph,
    Customer,
    EventMention,
    EventTagResult,
    LinkedEvent,
    LinkResult,
    RawClaim,
    Sentiment,
    STANCE_TYPES,
    STANCE_BEARING_TYPES,
    STREAMING_GROWABLE_TYPES,
    TAG_ONLY_TYPES,
    SourceBatch,
    SourceItem,
    StanceAssignment,
    StanceEntry,
    StanceProposal,
    StanceTagging,
    StanceType,
    StepSummary,
    TypeTriageItem,
    TypeTriageResult,
)
from src.entities.tags_gpt.persistence import load_content_graph, save_snapshot
from src.entities.tags_gpt.retrieval import ContentRetriever, EsNewsRetriever, LocalJsonRetriever
from src.entities.tags_gpt.streaming import StreamingState, StreamingTagsPipeline
from src.entities.tags_gpt.tagging import ClaimTagger, ClaimUpdater, StanceTagger, StanceUpdater, TypeTriageStep

__all__ = [
    "ArticleBundle",
    "ArticleProcessResult",
    "CachedJsonLlm",
    "ClaimCatalog",
    "ClaimCatalogStore",
    "ClaimTagger",
    "ClaimTagging",
    "ClaimUpdater",
    "ConsistencyPassResult",
    "ConsistencyPassStep",
    "ContentGraph",
    "ContentRetriever",
    "Customer",
    "default_cached_llm",
    "EsNewsRetriever",
    "EventMention",
    "EventStore",
    "EventTagResult",
    "group_by_source",
    "LinkedEvent",
    "LinkResult",
    "load_content_graph",
    "load_extracted_records",
    "LocalJsonRetriever",
    "OpenRouterJsonLlm",
    "RawClaim",
    "Sentiment",
    "STANCE_TYPES",
    "STANCE_BEARING_TYPES",
    "STREAMING_GROWABLE_TYPES",
    "TAG_ONLY_TYPES",
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
    "StanceType",
    "StanceUpdater",
    "StepSummary",
    "StreamingState",
    "StreamingTagsPipeline",
    "TypeTriageItem",
    "TypeTriageResult",
    "TypeTriageStep",
]
