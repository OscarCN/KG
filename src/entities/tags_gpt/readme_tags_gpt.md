# Tags GPT — Decoupled Streaming Implementation

This package is an alternate implementation of the tags flow in `src/entities/tags/`.
It keeps the same product goal — customer-anchored stances and per-event claim
clusters — but separates each pipeline step so it can be tested or moved into a
different component.

## Why this exists

The first tags implementation works end-to-end but couples too much orchestration
inside the runner and phase helpers. `tags_gpt` makes the boundaries explicit:

1. `extraction.py` — adapt already-extracted records into streamable batches.
2. `retrieval.py` — fetch article / post / comment source items.
3. `src/entities/linking_gpt/` — link extracted events and entities.
4. `linking_gpt.TagsGptLinkingAdapter` — expose linked events to the tags stream.
5. `tagging.py:TypeTriageStep` — classify stance ideas by type and extract catalog-free claims.
6. `tagging.py:StanceTagger` — assign current stances and propose catalog changes.
7. `tagging.py:StanceUpdater` — adjudicate/apply stance catalog changes.
8. `tagging.py:ClaimUpdater` — cluster/apply claims into per-event catalogs.

`streaming.py` is intentionally thin: it calls those steps in order for one
`SourceBatch`.

## Design Rules

- Models and catalogs are pure Python objects with no IO or LLM calls.
- Every LLM user depends on `JsonLlm`; tests can pass `ScriptedJsonLlm`.
- Stance tagging and claim tagging are separate calls.
- Stance updating and claim updating are separate mutation steps.
- `linking_gpt` owns event/entity creation and merge; tags only consume linked events.
- Snapshot writes are debug artifacts, not durable persistence.

## Minimal Usage

For step-by-step manual runs, edit the config block at the top of
`run_tags_gpt.py` and run:

```bash
ipython src/entities/tags_gpt/run_tags_gpt.py
```

or inside IPython/Jupyter:

```python
%run src/entities/tags_gpt/run_tags_gpt.py
```

For local exploratory runs, use the convenience runner:

```python
from pathlib import Path

from src.entities.tags_gpt.runner import LocalRunConfig, run_local_stream

result = run_local_stream(LocalRunConfig(
    extracted_records_path=Path("data/extracted_raw/ayuntamiento_tst.json"),
    customer_fixture_path=Path("data/tags/customer_75.json"),
    news_json_path=Path("data/ayuntamiento_qro/ayuntamiento_qro_20260506_015754.json"),
    snapshot_path=Path("data/tags/customer_75/tags_gpt_run.json"),
))
```

To wire the steps manually:

```python
from pathlib import Path

from src.entities.tags_gpt import (
    ClaimCatalogStore,
    ClaimTagger,
    ClaimUpdater,
    EventStore,
    LocalJsonRetriever,
    StanceCatalog,
    StanceTagger,
    StanceUpdater,
    StreamingState,
    StreamingTagsPipeline,
    TypeTriageStep,
    default_cached_llm,
    group_by_source,
    load_content_graph,
    load_extracted_records,
    sort_batches_by_publication,
)
from src.entities.linking_gpt import TagsGptLinkingAdapter

graph = load_content_graph(Path("data/tags/customer_75.json"))
customer = graph.customer
records = load_extracted_records(Path("data/extracted_raw/ayuntamiento_tst.json"))
batches = sort_batches_by_publication(group_by_source(records))

llm = default_cached_llm()
event_store = EventStore()
state = StreamingState(
    event_store=event_store,
    stance_catalog=StanceCatalog(customer.entity_id),
    claim_catalogs=ClaimCatalogStore(),
)

pipeline = StreamingTagsPipeline(
    state=state,
    retriever=LocalJsonRetriever(Path("data/ayuntamiento_qro/ayuntamiento_qro_20260506_015754.json")),
    linker=TagsGptLinkingAdapter(event_store=event_store),
    stance_tagger=StanceTagger(customer, llm),
    stance_updater=StanceUpdater(customer, llm),
    claim_tagger=ClaimTagger(customer, llm),
    claim_updater=ClaimUpdater(customer, llm),
    type_triage_step=TypeTriageStep(customer, llm),
)

for batch in batches:
    result = pipeline.process_batch(batch)
```

## Testing Pattern

Use `ScriptedJsonLlm` and a tiny retriever stub:

```python
from src.entities.tags_gpt import ScriptedJsonLlm

llm = ScriptedJsonLlm({
    "stance_tagging": {"assignments": [], "proposals": []},
    "claim_tagging": {"claims": []},
    "stance_update": {"decisions": []},
    "claim_update": {"decisions": [], "mutations": []},
})
```

For mutation-only tests, pass `llm=None` to `StanceUpdater` or `ClaimUpdater`:

- `StanceUpdater(..., llm=None)` accepts all proposed additions.
- `ClaimUpdater(..., llm=None)` creates one claim cluster per raw claim.

## Current Scope

- Manual tags_gpt runs use `src/entities/linking_gpt/` by default.
- `linking_gpt` preserves full event-linking behavior from `src/entities/linking/`
  and adds entity/concept linking by same `entity_type` plus shared name tokens,
  with LLM disambiguation over only `name` and `description`.
- Themes are still skipped.
- Tags are still applied only to linked events; linked entities are written for
  inspection and future graph use.
- This package is not wired into `src/entities/linking/run_linking.py` by default.
  It is meant to be the cleaner implementation target for the next runner or
  service split.
