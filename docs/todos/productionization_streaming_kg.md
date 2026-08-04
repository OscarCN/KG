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
- [x] **Apply the retrieval index migration** — applied to staging kgdb 2026-07-20 via `media-backend-paid/docs/db/migrations/staging_kgdb_catchup_2026-07-20.sql` (formerly `db/kg_db/add_retrieval_indexes.sql`)
  to live kgdb. **Applied to dev**; live pending. (All three kgdb migrations —
  retrieval indexes, `document_extractions`, `ontology_matching_rules` — are now
  on dev; none on live yet.)
- [ ] **Verify/apply on live:** P1 (`entity_locations` identity fix), P2
  (type-catalog seed via `scripts/gen_kg_catalog_seed.py`), and the
  `entities_documents.news_type` column.
- [x] **Persist per-document extractions. Done (dev).** `document_extractions`
  table + `KgdbWriter.write_extraction`; the listener writes one row per extracted
  record (pre-merge ground truth) — including the linker drops/skips that produce
  no `entities_documents` row. Idempotent on `(doc_id, record_hash)`;
  `reset_run` clears the tag's rows. Validated on dev (5-doc `--once`: all 7
  records incl. 2 skipped entities persisted; idempotent on rerun). DDL applied
  to dev kgdb. **Remaining:** apply to live (part of the migrations item above).
  See [persist_document_extractions.md](persist_document_extractions.md).
- [ ] **Provenance scheme** for `KG_RUN_TAG` in prod, so `reset_run(tag)` stays
  a usable per-batch/day rollback.

## Phase 2 — Config & secrets

- [ ] Prod config via k8s secret/configmap (not `.env.local`): `RABBIT_*`
  (prod vhost/queue/DLX), `KGDB_*` (live), `REDIS_*`, `OPENROUTER_*`,
  `GEOCODING_URL` (prod geocoder), and the TTLs
  (`KG_PROCESSED_TTL_SECONDS`, `KG_PROCESSING_TTL_SECONDS`).
- [ ] Point at **prod geocoder** and **prod Redis** (the dedup claim; and
  ideally a shared geocode cache — see Phase 4).

## Phase 3 — Producer (the missing half)

- [x] **gp3 firehose wired (code).** `KgStreamPipeline` (last in gp3's
  `NEWS_PIPELINES`) re-serializes the enriched doc to the ES-doc shape via
  `News.initialize_with_processor_message(...).format()`, whitelists to kg's
  `NEWS_FIELDS` + `_id`/`trace_id` (no embeddings), and POSTs to
  `rabbit_enqueuer` (`ENQUEUER_BACKEND_URL/enqueue`, queue **`kg_doc_stream`**
  via new `KG_QUEUE` env; unset = pipeline no-op, so gp3 deploys unchanged
  until flipped on). Publish failures log-and-swallow — gaps backfill via
  `enqueue_from_es.py`. Validated end-to-end: gp3 pipeline → real
  rabbit_enqueuer → dev queue → listener (geo scope pass → match → extract →
  link → `created:1` in dev kgdb, gp3 trace id throughout). **Remaining
  (deploy):** set `KG_QUEUE=kg_doc_stream` in gp3 prod; point the kg listener
  at the enqueuer's rabbit/vhost. NOTE: rabbit_enqueuer declares queues with
  no arguments — the listener must not set `RABBIT_DLX` for this queue
  (declare-args mismatch is a hard AMQP error); apply DLX later via a
  RabbitMQ **policy** instead.
- [x] **Demo geo scope, consumer-side.** The producer stays a dumb firehose;
  the listener drops out-of-scope docs before `Ontology.match` via
  `src/geo_scope.py` (`FILTER_GEO` env, comma-separated geoid prefixes; unset
  = no filter). Demo scope: CDMX municipios BJ/Cuauhtémoc/MH + Querétaro +
  Baja California (`_48409014,_48409015,_48409016,_48422,_48402`).
  `enqueue_from_es.py` shares the same rule (plus its coarse ES `cvegeo`
  pre-fetch derived from the scope). Verified equivalent to the script's
  previous inline rule on a 244-doc fixture (0 disagreements).
- [ ] Keep `enqueue_from_es.py` as the **backfill/test** producer; fix its
  `cvegeo` to OR + dedupe per municipality (currently ANDs both via
  `elastic_client`, discarding ~96% of the corpus).

## Phase 4 — Deployment & ops

- [x] **Dockerfile** — done, sibling-worker standard (`social_tags` pattern):
  single-stage, non-root `ejecutor`, `python -u src/listener.py` entrypoint.
  Deviations: `python:3.12-alpine` (pinned pandas/numpy stop at cp312 — build
  `--platform linux/amd64`), explicit writable `cache/`. The geocoder client
  is in-repo (`src/entities/linking/geocode.py` POSTs to `GEOCODING_URL`
  directly), so the former SSH-secret downloader stage pulling `apify_client`
  at a pinned commit — and its prod deploy-key requirement — is gone; no
  build secrets needed. Build/run/smoke commands in
  [`../../docker_examples`](../../docker_examples). Validated: containerized
  `--once` ran extract → geocode → link → persist against dev kgdb
  (`created:1`, then `reset_run`).
- [ ] **k8s deployment** for `kg` (via the `api_revival` k3s inventory, as the
  sibling workers deploy), resource limits, **1 replica** (per launch decision),
  `prefetch=1`, SIGTERM graceful shutdown (already implemented).
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
