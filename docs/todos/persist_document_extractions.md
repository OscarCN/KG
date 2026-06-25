# Persist per-document extractions (pre-merge ground truth)

## Problem

Today only the **canonical, merged** result is stored. The streaming listener
(`src/listener.py`) runs `extract → link_one → upsert_linked`, and persistence
writes:

- `entities.metadata` — the merged canonical record (fillna + most-precise-wins),
- `entities_documents` — just the `(entity, doc)` linkage (no extracted payload),
- `entity_types` / `entity_locations` / `event_properties`.

What each **individual document** actually said about an event — its own
description, date, location, price, casualties, etc., *before* the merge
collapses it — is **not persisted durably**. The only traces are the
`cache/extraction/` local files (ephemeral, per-pod, not queryable, gone in
k8s) and `data/extracted_raw/*.json` (written only by the batch/test harnesses,
not the listener). Anything the linker **drops or skips** (themes,
entities/concepts, records with no date) never even becomes an
`entities_documents` row — it is lost entirely.

## Why store it

- **Provenance / audit** — what every source claimed, independent of the merge.
- **Re-linking without re-paying the LLM** — if linking logic changes, re-run
  linking from stored extractions instead of re-extracting (the expensive step).
- **Training / eval data** and **debugging** the over/under-merges (we observed
  same-venue over-merge and national-event forking in testing).

## Design (locked: dedicated table)

A dedicated kgdb table, e.g. `document_extractions` — **one row per extracted
record**, capturing *everything* extracted (events, themes, entities — linked,
dropped, or skipped):

| Column | Purpose |
|---|---|
| `extraction_id` | PK (generated identity) |
| `doc_id` | source document id/url |
| `doc_index` | `news` / `comment` (shared vocabulary) |
| `supertype` | `_supertype` of the extracted record |
| `entity_type` | leaf `event_type` / `theme_type` / `entity_type` |
| `category` | `event` / `theme` / `entity` (from the schema) |
| `record` (`json`) | the **validated extracted record** (schema-conformant) |
| `linked_entity_id` | **nullable** — canonical `entities_alias.original_entity_id` it folded into; `NULL` when dropped/skipped |
| `link_status` | `created` / `merged` / `skipped` / `dropped` (+ reason) |
| `extraction_model` | OpenRouter model used |
| `prompt_variant` | `essn` / `full` |
| `run_tag` | `KG_RUN_TAG` provenance |
| `added` | timestamp |

Notes:
- Schema-first: DDL goes into `media-backend-paid/db/kg_db/schema.sql`
  (+ standalone migration), then applied to dev kgdb, then live.
- `linked_entity_id` uses the **alias `original_entity_id`** (not a hard FK), per
  the repo's alias-indirection convention, so entity merges stay stable.
- Index `doc_id` (re-link/audit by document) and `linked_entity_id` (all the raw
  takes that built one canonical).
- Rejected alternative: a JSON `extracted` column on `entities_documents` — only
  covers records that *linked* (loses drops/skips and the not-yet-linked
  themes/entities), so it can't be the full ground-truth store.

## Write path

In the listener's `KgPipeline.process` (and/or `KgdbWriter`), after each
`link_one`, write one `document_extractions` row for **every** extracted record —
including the ones the linker `skipped`/`dropped` (which currently produce no DB
row at all). For `created`/`merged`, set `linked_entity_id`/`link_status` from
the `LinkResult`. Keep it in the same per-message transaction as the canonical
upsert so it's atomic with the rest of the write (or a clearly-ordered sibling
write that also requeues on failure).

## Open questions

- One row per extracted record vs upsert by `(doc_id, supertype, entity_type)`
  on redelivery — likely upsert so a reprocessed document doesn't duplicate rows
  (the Redis claim already prevents most reprocessing).
- Retention: this table grows with the full firehose; decide a retention window
  or partition-by-time if volume warrants.

## Status

Design locked (dedicated table). Implementation pending — part of
[productionization_streaming_kg.md](productionization_streaming_kg.md) Phase 1.
Could ship **before** full go-live so we stop losing extraction data now.
