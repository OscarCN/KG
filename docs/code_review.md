# Code Review — Streaming kgdb Persistence

Date: 2026-06-24

Scope: static review of the streaming document pipeline, kgdb-backed candidate retrieval, event merge/write path, and persistence docs. Reviewed files include `src/listener.py`, `src/processed_store.py`, `src/entities/document.py`, `src/entities/extraction/extract.py`, `src/entities/linking/link.py`, `src/entities/linking/strategy.py`, `src/entities/linking/kgdb_retrieval.py`, `src/entities/linking/persistence.py`, producer/persist scripts, and the kgdb schema.

## Findings

### High — parallel workers can create duplicate canonicals for the same event

`EntityLinker._process()` does `lookup_candidates -> adjudicate -> create` outside any database lock, then `KgdbWriter._create()` inserts a fresh `entities` row with a random link id and no natural uniqueness guard. Two workers that process matching documents before either commit can both see no candidate and both create.

References:
- `src/entities/linking/link.py:200`
- `src/entities/linking/link.py:216`
- `src/entities/linking/persistence.py:261`

Suggested fix: add a DB-side coordination point before create, such as an advisory lock on a deterministic identity bucket `(supertype, geo/date bucket)` plus a second candidate lookup inside the lock, or an explicit reconciliation/merge transaction.

### High — concurrent merges can lose metadata updates

`KgdbRecordStore` reads `entities.metadata` without row locking, `strategy.merge()` mutates that local dict, and `_update()` overwrites the full metadata JSON. If two workers merge different source docs into the same canonical concurrently, the later commit can drop the earlier worker's `source_ids`, `_source_windows`, date choice, or promoted location from `entities.metadata`. `entities_documents` rows may both survive, but canonical metadata becomes stale/incomplete.

References:
- `src/entities/linking/kgdb_retrieval.py:104`
- `src/entities/linking/strategy.py:802`
- `src/entities/linking/persistence.py:291`

Suggested fix: in streaming upsert, lock the target `entities` row with `FOR UPDATE`, reload metadata, merge the incoming source into the locked latest record, then write.

### High — Redis document dedup is not an in-flight claim

The listener does `seen()` then processes then `mark()`. Two duplicate messages for the same `doc_id` can both pass `seen()` before either marks complete. That is exactly the multi-worker duplicate case the Redis layer is supposed to make cheap.

References:
- `src/listener.py:246`
- `src/processed_store.py:72`

Suggested fix: replace `seen/mark` with atomic states: `SET processing_key NX EX <short ttl>` before extraction, then set `processed_key` after commit and delete/release the processing key on failure.

### High — `_link_run` in `_find_existing()` can create duplicates across run tags/backfills

`KgdbRecordStore` retrieves candidates by `_link_id` globally, but `KgdbWriter._find_existing()` only treats a row as existing if both `_link_id` and `_link_run` match. If a new stream/backfill run merges into a canonical written under another run tag, `upsert_linked()` may create a second `entities` row with the same logical `_link_id` under the new run.

References:
- `src/entities/linking/kgdb_retrieval.py:106`
- `src/entities/linking/persistence.py:305`

Suggested fix: streaming upsert should find by `_link_id` alone, or enforce a unique expression index on `metadata->>'_link_id'` for canonical KG rows. Keep `_link_run` as provenance/reset scope, not identity.

### Medium — `entities_documents.doc_date_created` is wrong for merged sources

`_write_documents()` uses canonical `record["publication_date"]` for every `source_id`. But `strategy.merge()` intentionally keeps the earliest publication date across sources. Any later source added to the event gets the earliest date, not its own document date, which breaks period filters and reporting.

References:
- `src/entities/linking/strategy.py:820`
- `src/entities/linking/persistence.py:246`

Suggested fix: store source-level metadata, e.g. `_sources: [{source_id, publication_date, news_type}]`, and write `entities_documents` from that.

### Medium — `news_type` is never propagated into linked records

`record_to_article()` returns `document_type`, but not `news_type`; extraction only preserves `_source_id`, `_supertype`, and `date_created`; `_write_documents()` reads `record.get("news_type")`, which will usually be `None`.

References:
- `src/entities/document.py:57`
- `src/entities/extraction/extract.py:811`
- `src/entities/linking/persistence.py:247`

Suggested fix: carry `news_type` or normalized source type through article -> extracted record -> linked source metadata.

### Medium — prompt `{source_type}` is effectively always `news` in the streaming path

`record_to_article()` sets `document_type`, but `_build_extraction_messages()` reads `article.get("source_type", "news")`. Social/date inference instructions therefore get the wrong context unless the input happens to include `source_type`.

References:
- `src/entities/document.py:63`
- `src/entities/extraction/extract.py:719`

Suggested fix: either set `source_type = doc_type` in `record_to_article()` or read `document_type` in prompt context.

### Medium — kgdb candidate lookup may not scale without indexes matching the actual query

`KgdbCandidateIndex` filters on JSON metadata, `event_properties` range overlap, and `entity_locations` level/grid columns. The checked schema shows only `idx_ed_entity_docindex_date`; no visible expression/GIN/GiST indexes for `metadata->>'_supertype'`, `metadata->>'event_type'`, `event_properties` range, or location level ids.

References:
- `src/entities/linking/kgdb_retrieval.py:81`
- `/Users/oscarcuellar/ocn/media/media-backend-paid/db/kg_db/schema.sql:537`

Suggested fix: add migrations for the actual retrieval predicates, or switch the type partition filter to `entity_types` as the docs claim.

### Low — docs are stale/inconsistent around streaming status

`README.md` says streaming + kgdb-backed retrieval are implemented and validated, while `src/entities/readme_entities.md`, `src/listener.py` docstring, and `src/entities/linking/readme_linking.md` still say streaming or kgdb-backed candidate retrieval is pending/in-memory.

References:
- `README.md:121`
- `src/entities/readme_entities.md:7`
- `src/listener.py:17`
- `src/entities/linking/readme_linking.md:228`

## Residual Risk

The current design is robust to simple restart after committed writes when `KG_RUN_TAG` stays constant. It is not yet robust to true parallelism: the missing DB-side lock/merge primitive is the main gap.

This review was static only. It did not run integration tests or hit RabbitMQ/Postgres.
