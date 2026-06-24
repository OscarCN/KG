# TODO — Document retrieval strategy (keyword pre-filter → kg doc queue)

**Status:** design — decided **global ontology KG via a new retriever**; not implemented
**Area:** new retriever component (ES query from `keywords.xlsx` → RabbitMQ doc queue);
consumes nothing in `kg` but feeds `src/listener.py`
**Related:** [`active_type_extraction.md`](active_type_extraction.md),
[`kgdb_event_persistence.md`](kgdb_event_persistence.md),
[`../../src/entities/extraction/readme_extraction.md`](../../src/entities/extraction/readme_extraction.md),
`src/PoC/get_entities_data.py` (keywords→ES prototype), shared lib `elastic_client`

## Problem

The streaming listener (`src/listener.py`) assumes documents are handed to it, but **nothing
selects them**. Keyword matching (`Ontology.match` over `keywords.xlsx`) runs *inside the worker
per document* — a per-doc filter, not a retrieval strategy. Keyword matching is fundamentally an
**ES query**; running the whole news firehose through the worker just to discard non-matches is
wasteful and has no actual producer.

## Decision

**Global ontology KG.** kg extracts from **all** news matching the ontology keywords (not scoped
to customer saved searches — that's the `search-sentiment-poller-tasker`'s separate job). A
**new, dedicated retriever** compiles `keywords.xlsx` into an ES query, polls ES, and publishes
matching documents to the kg **doc queue** that the listener consumes. Independent of saved
searches; two distinct selection criteria (ontology keywords vs. per-customer saved search) stay
separate.

## Design — two-stage filter

1. **Coarse retrieve (ES, the new piece).** Compile the **active** `keywords.xlsx` rows into one
   ES `bool` query and poll ES on an interval:
   - per row → `must` (any `kw` stemmed / `phrase` exact) ∧ `must_not` (`not`, `dismiss_categories`)
     ∧ `filter` (`categories`, `document_type`); **OR** across rows.
   - **active only** — only rows enabled per [`active_type_extraction.md`](active_type_extraction.md)
     (today the `enabled` column; later the kgdb catalog `active` flag) enter the query.
   - go through **`elastic_client`** (the shared `search_query`→ES lib) — do **not** hand-roll ES.
   - `src/PoC/get_entities_data.py` already translates *one* `keywords.xlsx` row → an ES request;
     generalize to the OR-of-all-active-rows and add incremental polling.
2. **Precise route (worker, already built).** The listener still runs `Ontology.match` as the
   *per-class router* — the ES OR is coarse (recall); the per-row ANDs + class assignment are the
   precise step. So `Ontology.match` is not removed, just demoted from "scan the firehose" to
   "assign classes to an already-relevant doc."

```
ES news index ──(active keywords.xlsx → bool query via elastic_client, polled)──▶ retriever
   ──(publish matching docs)──▶ RabbitMQ doc queue ──▶ src/listener.py
   (Ontology.match → classify → extract → link → KgdbWriter.upsert_linked)
```

## Components & contract

- **Retriever** (new): periodic ES poll → publish one message per matching document to the kg doc
  queue. Message = a raw document in the shape `record_to_article` expects (news-style flat or
  Facebook-style nested). Carry a top-level `trace_id` (dev convention).
- **Where it lives:** a producer that queries ES and publishes to RabbitMQ — a crawler/`cargas`-shaped
  role. Could be its own repo/service or a `kg` script; **recommend its own deployable** so the
  `kg` worker stays consume-only. (Decide during implementation.)
- **Listener:** unchanged — already consumes the doc queue and runs the full pipeline.

## Open questions

- **Watermark / no re-processing.** The retriever must track a cursor (e.g. max `article_date` /
  ingestion timestamp seen) so each poll enqueues only *new* docs — re-extracting is wasteful and
  re-bills the LLM even though kgdb dedup makes it idempotent. Cursor store + poll cadence TBD.
- **Keyword→ES fidelity.** `kw` uses NLTK Spanish stemming in the worker; ES needs an analyzer that
  matches (or accept coarser recall at the ES stage, with the worker's exact `Ontology.match` as
  the precise gate). `phrase` = exact, `not`/`dismiss` = `must_not`, `categories`/`document_type`
  = filters — confirm these all express in `elastic_client`'s `search_query` spec.
- **Backfill vs. live.** One-shot historical backfill (date-ranged query) vs. steady incremental
  polling — likely both, sharing the compiled query.
- **Single source of truth for keywords.** Keep `keywords.xlsx` authoritative, or move rules into
  the DB alongside the `active` flag ([`active_type_extraction.md`](active_type_extraction.md)) so
  the retriever and the worker read the same catalog. Leaning DB-authoritative long-term.
- **Relationship to the poller.** Confirm the global retriever and `search-sentiment-poller-tasker`
  stay separate producers (ontology corpus vs. per-customer tasks), both ultimately enriching kgdb.

## Sequencing

Independent of the linking/persistence work (that's the consume side, done). Natural next step to
make the streaming pipeline *fed* rather than hand-published. Prereq for any real-volume run.
