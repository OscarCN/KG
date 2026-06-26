# Productionization: streaming KG → live kgdb

Go-live checklist for running the streaming listener (`src/listener.py`:
`classify → extract → link → persist`) continuously against the **live kgdb**.
This is a release checklist; the granular design work lives in the per-item
TODOs linked below.

## Decisions (locked)

- **Environment model: production-only.** kg writes to **kgdb**, the shared,
  append-only ground-truth DB — *not* userdb. Running staging+production writers
  into one kgdb would double-write, so there is **one continuous writer into
  live kgdb**. The **dev kgdb** (Docker Postgres `:5334`, see
  `media/dev/docs/db/runbook.md`) is the pre-prod test target. *(Open: confirm
  no separate staging kgdb exists.)*
- **Launch concurrency: single worker first.** Known gap (deferred review
  finding): true parallel workers can mint duplicate canonicals because the
  linker's `lookup → adjudicate → create` is not under a DB lock. Launch with
  **one listener** (no parallel-create race), validate ground truth, and scale
  to N workers only after [canonical reconciliation](canonical_reconciliation.md)
  lands. This fixes `prefetch`/replica count at 1 for v1.
- **Producer scope: post-`gp3` firehose MVP** — stream the enriched firehose
  into the kg doc queue and let the in-worker `Ontology.match` pre-filter (no
  LLM in matching). See [document_retrieval_strategy.md](document_retrieval_strategy.md).
- **Quality bar: accept v1.** The under/over-merges observed in testing
  (national/no-location events fork; same-venue over-merge; geocoder leaf
  twin-fork) are accepted for v1 ground truth and tracked post-go-live.

## Phase 1 — Schema & data in live kgdb

Schema-first: all DDL goes through `media-backend-paid/db/kg_db/schema.sql`
(+ a standalone migration file), then applied to live.

- [x] **Ontology keywords → kgdb table. Done (dev).** `ontology_matching_rules`
  holds every rule (raw/human-editable list columns `kw`/`phrase`/`not_kw`/
  `categories`/`dismiss_categories`/`document_type` + `enabled` + labels).
  `Ontology` loads from kgdb when `KG_ONTOLOGY_SOURCE=db` (Excel stays the
  dev/test default), normalizing at load so matching is byte-identical (verified:
  same 47 enabled classes, identical rule set, identical match output on the
  fixture). Seeded by `scripts/seed_ontology_rules.py` (full refresh from
  `keywords.xlsx`); DDL applied to dev kgdb. **Remaining:** apply to live; a
  proper edit path (SQL now, admin UI later); and the **`active` gate** sourced
  from the type catalog ([active_type_extraction.md](active_type_extraction.md)),
  which elevates today's `enabled` gate.
- [ ] **Apply the retrieval index migration** (branch
  `persistence-review-kgdb-indexes` in `media-backend-paid`) to live kgdb.
- [ ] **Verify/apply on live:** P1 (`entity_locations` identity fix), P2
  (type-catalog seed via `scripts/gen_kg_catalog_seed.py`), and the
  `entities_documents.news_type` column.
- [ ] **Persist per-document extractions.** Add the `document_extractions`
  table (pre-merge ground truth) and have the listener write one row per
  extracted record — including linker drops/skips, which currently produce no DB
  row at all. See [persist_document_extractions.md](persist_document_extractions.md).
  Can ship before full go-live so we stop losing extraction data now.
- [ ] **Provenance scheme** for `KG_RUN_TAG` in prod, so `reset_run(tag)` stays
  a usable per-batch/day rollback.

## Phase 2 — Config & secrets

- [ ] Prod config via k8s secret/configmap (not `.env.local`): `RABBIT_*`
  (prod vhost/queue/DLX), `KGDB_*` (live), `REDIS_*`, `OPENROUTER_*`,
  `NLP_URL`/`GEOCODING_URL` (prod geocoder), and the TTLs
  (`KG_PROCESSED_TTL_SECONDS`, `KG_PROCESSING_TTL_SECONDS`).
- [ ] Point at **prod geocoder/NLP** and **prod Redis** (the dedup claim; and
  ideally a shared geocode cache — see Phase 4).

## Phase 3 — Producer (the missing half)

- [ ] Wire `gp3`'s post-enrichment output to also publish to the **kg doc
  queue** (fan-out exchange / extra binding, or a small bridge consumer).
  Message shape = the enriched ES doc (same as `scripts/enqueue_from_es.py`
  publishes). **Cross-repo seam — touches `gp3`.**
- [ ] Keep `enqueue_from_es.py` as the **backfill/test** producer; fix its
  `cvegeo` to OR + dedupe per municipality (currently ANDs both via
  `elastic_client`, discarding ~96% of the corpus).

## Phase 4 — Deployment & ops

- [ ] **Dockerfile + k8s deployment** for `kg` (via the `api_revival` k3s
  inventory, as the sibling workers deploy), resource limits, **1 replica**
  (per launch decision), `prefetch=1`, SIGTERM graceful shutdown (already
  implemented).
- [ ] **Cache strategy.** `cache/{geocode,extraction,link_llm}` are local files
  — unshared across pods. Decide ephemeral (re-bill) vs Redis/shared volume for
  at least the geocode cache (highest reuse).
- [ ] **Observability.** Logs already carry `trace_id`; add DLX/dead-letter
  alerting, `created/merged/skipped/dropped` + `no_match` metrics, a sink for
  the case log, and liveness/readiness probes.
- [ ] **Cost controls.** OpenRouter budget/rate limits. Essential-extraction
  default is on; the on-demand enrichment trigger
  ([tiered_extraction_essential_fields.md](tiered_extraction_essential_fields.md))
  stays deferred.

## Phase 5 — Validation & cutover

- [ ] Dev kgdb smoke (done at small scale) → **bounded live canary** (1 worker,
  small ES window via `enqueue_from_es.py`) → inspect kgdb → enable continuous.
- [ ] **Rollback:** `KgdbWriter.reset_run(tag)` + pause the producer.

## Phase 6 — Post-go-live (quality)

- [ ] [canonical_reconciliation.md](canonical_reconciliation.md) — also unblocks
  multi-worker scaling.
- [ ] [location_level_list_extraction.md](location_level_list_extraction.md),
  [retrieval_name_soft_type.md](retrieval_name_soft_type.md).
- [ ] **New:** national / no-location event identity — events with no specific
  place (e.g. a nationwide protest) fork into many canonicals because the hard
  geo gate makes *noloc* incompatible with everything. No TODO file yet.
