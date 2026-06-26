# TODO — Document retrieval strategy (keyword pre-filter → kg doc queue)

**Status:** design — decided **global ontology KG**; **MVP = stream everything + in-worker keyword
pre-filter** (ES-query retriever deferred as a scale optimization); not implemented
**Area:** a `gp3` fanout → kg doc queue (MVP); later an ES-query retriever. Feeds `src/listener.py`
(unchanged)
**Related:** [`active_type_extraction.md`](active_type_extraction.md),
[`kgdb_event_persistence.md`](kgdb_event_persistence.md),
[`../extraction.md`](../extraction.md),
`src/PoC/get_entities_data.py` (keywords→ES prototype), shared lib `elastic_client`

## Problem

The streaming listener (`src/listener.py`) assumes documents are handed to it, but **nothing
selects them**. Keyword matching (`Ontology.match` over `keywords.xlsx`) runs *inside the worker
per document* — a per-doc filter, not a retrieval strategy. Keyword matching is fundamentally an
**ES query**; running the whole news firehose through the worker just to discard non-matches is
wasteful and has no actual producer.

## Decision

**Global ontology KG.** kg extracts from **all** news matching the ontology keywords (not scoped
to customer saved searches — that's the `search-sentiment-poller-tasker`'s separate job). Two
distinct selection criteria (ontology keywords vs. per-customer saved search) stay separate.

**MVP = stream everything, pre-filter in the worker.** The listener already runs `Ontology.match`
(keyword/stem matching, **no LLM**), so the simplest path is to feed it the **post-`gp3` enriched
document firehose** and let it discard non-matches. Crucially, **the LLM cost is identical either
way** — only keyword-matched docs ever reach the extraction LLM — so an ES pre-filter saves only
RabbitMQ throughput + worker CPU on non-matches, not the expensive part. The **ES-query retriever
below is deferred** to when volume/cost justifies it.

## Design (MVP) — stream + in-worker filter

```
gp3 (enriched docs) ──(fanout)──▶ RabbitMQ doc queue ──▶ src/listener.py
   (Ontology.match keyword pre-filter → classify → extract → link → KgdbWriter.upsert_linked)
```

- **Tap the post-`gp3` enriched stream**, not raw ingestion: text rules (`kw`/`phrase`) work on
  any doc, but `categories`/`dismiss_categories` rules need the enrichment `gp3` adds. Bind a kg
  queue to a gp3 fanout/publish point (small change on gp3's side) so category rules function.
- **Listener unchanged** — `Ontology.match` stays the keyword pre-filter; it just runs over the
  full stream instead of a pre-selected set.
- **Scale by adding listeners** — cross-worker dedup is done (kgdb `CandidateIndex`), so N
  consumers on the queue won't duplicate.

## Later optimization — ES-query retriever (two-stage filter)

When the firehose volume makes "match in worker" wasteful, move the coarse filter to an ES query:

1. **Coarse retrieve (ES).** Compile the **active** `keywords.xlsx` rows into one ES `bool` query,
   polled on an interval:
   - per row → `must` (any `kw` stemmed / `phrase` exact) ∧ `must_not` (`not`, `dismiss_categories`)
     ∧ `filter` (`categories`, `document_type`); **OR** across rows.
   - **active only** — per [`active_type_extraction.md`](active_type_extraction.md) (`enabled`
     column today; the kgdb catalog `active` flag later).
   - via **`elastic_client`** (the shared `search_query`→ES lib) — do **not** hand-roll ES.
   - `src/PoC/get_entities_data.py` already translates *one* `keywords.xlsx` row → an ES request;
     generalize to the OR-of-all-active-rows + incremental polling.
2. **Precise route (worker).** `Ontology.match` stays as the per-class router (the ES OR is coarse
   recall; the per-row ANDs + class assignment are the precise step).

## Testing producer (exists)

`scripts/enqueue_from_es.py` is the interim hand-run producer for a city-or-two test corpus:
ES date-window fetch (`period=[start,end]`) scoped by `cvegeo` to `level_2_id ∈ {48409, 48422}`
(`location_type="mentioned"`), then keeps only docs whose **same `locations_mentioned` entry** has
one of those ids **and** `precision_level >= 3`, drops category `Deportes`, and publishes each to
`RABBIT_QUEUE` with a `trace_id`. No keyword filter (that stays in the listener). `--dry-run`
validates the filter first. This is the throwaway stand-in for the MVP fanout / ES retriever below.

## MVP work (small)

- **Fanout tap on `gp3`** (cross-repo): publish enriched docs to an exchange the kg queue binds to
  (a copy of what already goes to ES/topics). The doc message must be in the shape
  `record_to_article` expects (news-style flat or Facebook-style nested); carry a top-level
  `trace_id`. The listener itself is **unchanged**.
- That's it for the MVP — no retriever, no cursor, no ES-query compilation.

## Open questions

- **Firehose tap point.** Bind to the **post-`gp3` enriched** stream (so `categories` rules work),
  not raw ingestion. Needs a small fanout/publish on `gp3` — confirm its exchange topology.
- **Backpressure at volume.** In-worker `Ontology.match` over the full firehose is cheap but not
  free; if it ever dominates, that's the trigger to build the ES-query retriever (the optimization
  above). Scale-out (N listeners) is the cheaper first lever — dedup is cross-worker now.
- **Keyword→ES fidelity** *(only when the ES retriever is built)*: `kw` uses NLTK Spanish stemming
  in the worker; ES needs a matching analyzer (or accept coarser ES recall with `Ontology.match` as
  the exact gate). `phrase` = exact, `not`/`dismiss` = `must_not`, `categories`/`document_type` =
  filters — confirm these express in `elastic_client`'s `search_query` spec.
- **Backfill** *(ES retriever)*: a one-shot date-ranged historical query vs. steady live streaming —
  the stream-all MVP only covers *new* docs, so a backfill still wants the ES path.
- **Single source of truth for keywords.** Keep `keywords.xlsx` authoritative, or move rules into
  the DB alongside the `active` flag ([`active_type_extraction.md`](active_type_extraction.md)).
- **Relationship to the poller.** The global kg stream and `search-sentiment-poller-tasker` stay
  separate producers (ontology corpus vs. per-customer tasks), both ultimately enriching kgdb.

## Sequencing

Independent of the linking/persistence work (consume side, done). The MVP (gp3 fanout → kg queue)
is the natural next step to make the pipeline *fed* rather than hand-published. The ES-query
retriever is a later optimization, gated on volume, and also the home for historical backfill.
