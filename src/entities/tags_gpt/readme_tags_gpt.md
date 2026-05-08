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
3. `candidates.py` — retrieve plausible linked-event candidates.
4. `linking.py` — choose merge vs create for one extracted event.
5. `tagging.py:StanceTagger` — assign current stances and propose catalog changes.
6. `tagging.py:StanceUpdater` — adjudicate/apply stance catalog changes.
7. `tagging.py:ClaimTagger` — extract customer-affecting raw claims.
8. `tagging.py:ClaimUpdater` — cluster/apply claims into per-event catalogs.

`streaming.py` is intentionally thin: it calls those steps in order for one
`SourceBatch`.

## Design Rules

- Models and catalogs are pure Python objects with no IO or LLM calls.
- Every LLM user depends on `JsonLlm`; tests can pass `ScriptedJsonLlm`.
- Stance tagging and claim tagging are separate calls.
- Stance updating and claim updating are separate mutation steps.
- Linking owns event creation/merge; tags only consume linked events.
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

from src.entities.tags_gpt import LocalRunConfig, run_local_stream

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
    EventLinkingStep,
    EventStore,
    LocalJsonRetriever,
    StanceCatalog,
    StanceTagger,
    StanceUpdater,
    StreamingState,
    StreamingTagsPipeline,
    default_cached_llm,
    group_by_source,
    load_content_graph,
    load_extracted_records,
    sort_batches_by_publication,
)

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
    linker=EventLinkingStep(event_store=event_store),
    stance_tagger=StanceTagger(customer, llm),
    stance_updater=StanceUpdater(customer, llm),
    claim_tagger=ClaimTagger(customer, llm),
    claim_updater=ClaimUpdater(customer, llm),
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

- Event linking is in-memory and intentionally simple.
- Candidate retrieval covers linked events, not themes/entities yet.
- Geocoding is not performed here; the linker uses `_geo.level_2_id` when present
  and falls back to the structured location state/country.
- This package is not wired into `src/entities/linking/run_linking.py` by default.
  It is meant to be the cleaner implementation target for the next runner or
  service split.
