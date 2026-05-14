# Tags subsystem — userdb schema (final) + migration plan

## Context

The tags subsystem (`src/entities/tags/`) runs in-memory today. To support a long-running RabbitMQ consumer that survives crashes, we need to durably store stance entries, stance assignments, claim clusters, claim assignments, and per-`(entity, org)` consistency-pass state in **userdb**.

This document captures the schema decisions. The companion migration script (`media-backend-paid/docs/social_tags_schema_update_userdb.sql`) is what you run by hand against userdb. Once applied, the DDL is folded into `media-backend-paid/db/user_db/schema.sql` per the userdb workflow.

## Settled decisions

- **Database**: userdb (`local-backend-postgres-server`).
- **Catalog scope**: per `(entity_id, org_id)`. `query_id` is denormalised on each assignment for traceability — not part of the catalog key. All saved searches inside one org share one stance/claim catalog over a given entity.
- **`entity_id` semantics**: like `entities_documents_sentiments_org.entity_id`, points at `kgdb.entities_alias.original_entity_id`. Cross-DB, no DB FK.
- **Source-item text**: not persisted. Consistency pass re-fetches text from ES `news` (posts + embedded comments) by `source_item_id`. ES retention covers the 50–200 bundles between passes.
- **Stance retention**: `assignment_ttl_days` per `(entity_id, org_id)`, default 4 (spec range 3–5). Old stance assignments are deleted; stances left with zero assignments are then deleted.
- **Claim retention**: **none**. Claims are scoped to their event — when the event stops streaming, its claims and clusters stay as they were. No TTL job touches `claim_clusters` / `claim_assignments`.
- **`origin_event_id` on stance_entries**: **dropped** (nothing reads it; in-memory model field is also unused).

## Tables

### 1. `stance_entries`

| Column | Type | Notes |
|---|---|---|
| `stance_id` | TEXT PK | e.g. `complaint__demora-pago__a1b2c3` (matches `make_entry_id`) |
| `entity_id` | INTEGER NOT NULL | → `kgdb.entities_alias.original_entity_id` (app-level FK) |
| `org_id` | INTEGER NOT NULL | → `orgs.org_id` |
| `label` | TEXT NOT NULL | |
| `description` | TEXT NOT NULL DEFAULT '' | |
| `primary_type` | TEXT NOT NULL | StanceType |
| `aliases` | JSONB NOT NULL DEFAULT '[]' | rename/merge history |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Constraints:
- Unique: `(entity_id, org_id, primary_type, label)`.

Indexes:
- `idx_stance_entries_scope (entity_id, org_id, primary_type)`.

Lifecycle: inserted by streaming tagger / bootstrap / consistency Stage 2; hard-deleted by retention when zero assignments remain.

### 2. `stance_assignments`

| Column | Type | Notes |
|---|---|---|
| `record_id` | BIGSERIAL PK | |
| `source_item_id` | TEXT NOT NULL | post URL or comment id |
| `source_kind` | TEXT NOT NULL | `article` / `user_post` / `user_comment` |
| `parent_source_id` | TEXT NULL | Post-level URL. For `source_kind='user_comment'` → the parent post's URL; for root rows (`article` / `user_post`) → the row's own `source_item_id`. **Diverges from `entities_documents_sentiments_org.parent_doc_id`** (which is NULL for roots) so per-post aggregations don't need `COALESCE`. |
| `news_type` | TEXT NULL | Social-network identifier (`facebook` / `instagram` / `x` / `linkedin` / `tiktok` / `news` / `impreso` / `radio` / `tv`). Comment rows inherit the parent post's `news_type` since the comment's own metadata doesn't carry it. |
| `entity_id` | INTEGER NOT NULL | → `entities_alias.original_entity_id` |
| `org_id` | INTEGER NOT NULL | |
| `query_id` | INTEGER NULL | → `user_searches.query_id`; denormalised, not in unique key |
| `stance_id` | TEXT NULL | NULL = orphan (Stage 2 input) |
| `stance_type` | TEXT NOT NULL | one of StanceType |
| `event_id` | TEXT NULL | ES topic_id; NULL when stance is event-independent |
| `reason` | TEXT NOT NULL DEFAULT '' | triage's `brief_summary` |
| `assigned_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Constraints:
- Unique: `(source_item_id, entity_id, org_id, stance_type)` — at most one row per item per stance type per `(entity, org)`, across NULL and non-NULL `stance_id`. Re-tagging (re-crawl, queue redelivery) flows through `INSERT … ON CONFLICT DO UPDATE` so the latest decision overwrites the previous row's `stance_id` / `reason` / `assigned_at` / `event_id`; item-context columns (`source_kind`, `parent_source_id`, `news_type`, `query_id`) stay because they describe the item, not the decision.
- FK on `stance_id` → `stance_entries(stance_id)` `ON DELETE RESTRICT` — guards against accidental cascade-delete; app code must clean assignments before removing an entry.

Indexes:
- `idx_stance_assignments_recent (entity_id, org_id, stance_type, assigned_at DESC)` — backs `recent_bundle_assignments` and per-type counts.
- `idx_stance_assignments_stance (stance_id)` — backs merge/retire cascade and per-stance count subqueries.
- `idx_stance_assignments_dedup (entity_id, org_id, source_item_id)` — "did we already tag this item?".

### 3. `claim_clusters`

| Column | Type | Notes |
|---|---|---|
| `cluster_id` | TEXT PK | matches `make_cluster_id` |
| `entity_id` | INTEGER NOT NULL | |
| `org_id` | INTEGER NOT NULL | |
| `event_id` | TEXT NOT NULL | ES topic_id |
| `canonical` | TEXT NOT NULL | |
| `aliases` | JSONB NOT NULL DEFAULT '[]' | rename/merge history |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| `is_new` | BOOLEAN NOT NULL DEFAULT TRUE | |
| `freshness_window_hours` | INTEGER NOT NULL DEFAULT 24 | |

Constraints:
- Unique: `(entity_id, org_id, event_id, canonical)`.

Indexes:
- `idx_claim_clusters_scope (entity_id, org_id, event_id)`.

No TTL — clusters persist for the lifetime of the event.

### 4. `claim_assignments`

| Column | Type | Notes |
|---|---|---|
| `record_id` | BIGSERIAL PK | |
| `source_item_id` | TEXT NOT NULL | |
| `source_kind` | TEXT NOT NULL | |
| `parent_source_id` | TEXT NULL | Post-level URL. Comment → parent post URL; root → own `source_item_id`. Same convention as `stance_assignments.parent_source_id` (diverges from `entities_documents_sentiments_org`). |
| `news_type` | TEXT NULL | Social-network identifier. Comment rows inherit the parent post's `news_type`. |
| `entity_id` | INTEGER NOT NULL | |
| `org_id` | INTEGER NOT NULL | |
| `query_id` | INTEGER NULL | traceability |
| `event_id` | TEXT NOT NULL | |
| `cluster_id` | TEXT NOT NULL | FK → `claim_clusters(cluster_id)` `ON DELETE RESTRICT` |
| `verbatim` | TEXT NOT NULL | |
| `verbatim_hash` | TEXT NOT NULL | sha256 hex of `verbatim`; backs the idempotency unique index. Computed app-side by `db.py::_verbatim_hash`. |
| `importance` | INTEGER NOT NULL DEFAULT 1 | 1/2/3 |
| `importance_reason` | TEXT NOT NULL DEFAULT '' | |
| `extracted_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Indexes / constraints:
- Unique: `idx_claim_assignments_uniq (source_item_id, entity_id, org_id, event_id, cluster_id, verbatim_hash)` — backs `INSERT … ON CONFLICT DO NOTHING` so a redelivered bundle (RabbitMQ at-least-once) can't duplicate claims.
- `idx_claim_assignments_cluster (cluster_id)`.
- `idx_claim_assignments_scope (entity_id, org_id, event_id)`.
- `idx_claim_assignments_recent (entity_id, org_id, extracted_at DESC)`.

No TTL on claim assignments either.

### 5. `tags_entity_state`

| Column | Type | Notes |
|---|---|---|
| `entity_id` | INTEGER NOT NULL | |
| `org_id` | INTEGER NOT NULL | |
| `bundles_processed_total` | INTEGER NOT NULL DEFAULT 0 | one bundle = one root post/article + its comments (incremented once per `process_bundle` call) |
| `bundles_processed_since_last_pass` | INTEGER NOT NULL DEFAULT 0 | zeroed by `mark_consistency_pass` |
| `last_consistency_pass_at` | TIMESTAMPTZ NULL | |
| `last_consistency_pass_count` | INTEGER NOT NULL DEFAULT 0 | |
| `bootstrap_completed_at` | TIMESTAMPTZ NULL | streaming refuses to start until set |
| `assignment_ttl_days` | INTEGER NOT NULL DEFAULT 4 | stance retention only; configurable per `(entity, org)` |
| `consistency_pass_threshold_bundles` | INTEGER NOT NULL DEFAULT 200 | drives `Customer.consistency_pass_due` against `bundles_processed_since_last_pass` |
| `consistency_pass_threshold_days` | INTEGER NOT NULL DEFAULT 7 | |

Constraints:
- PK: `(entity_id, org_id)`.

## Mutation cascade rules

The streaming pipeline and consistency pass only call the catalog method surface. Every mutation is one TX:

| Mutation | SQL |
|---|---|
| `add(label, description, *, primary_type)` | `INSERT INTO stance_entries … RETURNING *` |
| `assign(StanceAssignment)` | `INSERT INTO stance_assignments … ON CONFLICT DO NOTHING`; rejects if `stance_id` set and entry's `primary_type` ≠ `stance_type` |
| `rename(stance_id, label, description)` | `UPDATE stance_entries SET label, description, aliases = aliases \|\| jsonb_build_array(old_label) WHERE stance_id=:id` |
| `merge(src, dst)` | TX: `UPDATE stance_assignments SET stance_id=:dst WHERE stance_id=:src`; `UPDATE stance_entries SET aliases = aliases \|\| jsonb_build_array(src.label) WHERE stance_id=:dst`; `DELETE FROM stance_entries WHERE stance_id=:src` |
| `reroute(from, to)` | `UPDATE stance_assignments SET stance_id=:to WHERE stance_id=:from` |
| `delete(stance_id)` (retention) | `DELETE FROM stance_entries WHERE stance_id=:id AND NOT EXISTS (SELECT 1 FROM stance_assignments WHERE stance_id=:id)` |

Claim equivalents follow the same shape — `rename`/`merge` on `claim_clusters`; `assign` inserts into `claim_assignments`.

## Retention (stances only)

Two SQL statements per `(entity, org)` per consistency pass, run **before** Stages 1–3:

```sql
-- 1. Expire old stance assignments.
DELETE FROM stance_assignments
WHERE entity_id = :e AND org_id = :o
  AND assigned_at < now() - (:ttl_days || ' days')::interval;

-- 2. Hard-delete orphan stance entries.
DELETE FROM stance_entries
WHERE entity_id = :e AND org_id = :o
  AND NOT EXISTS (
      SELECT 1 FROM stance_assignments
      WHERE stance_id = stance_entries.stance_id
  );
```

Claims are untouched.

## Files

1. **`src/entities/tags/serialization_plan.md`** (this file) — project-side reference.
2. **`media-backend-paid/docs/social_tags_schema_update_userdb.sql`** — standalone migration; apply manually on existing environments.
3. **`media-backend-paid/db/user_db/schema.sql`** — same DDL folded in as the source-of-truth schema (new environments get it for free on first reflect).
4. **`src/entities/tags/db.py`** — `StanceCatalogRepo`, `ClaimCatalogStoreRepo` / `ClaimCatalogRepo`, `EntityStateRepo`, `connect_userdb()`. Drop-in replacement for the in-memory catalogs.
5. **`src/entities/tags/source_items.py`** — `LocalFileSourceItemFetcher` (used by the simulated stream) and `ESSourceItemFetcher` (production path). Both implement `fetch_for_assignments(assignments) -> dict[id, SourceItem]`.
6. **`src/entities/tags/stream.py`** — file-simulated, userdb-backed streaming entry point. Top-level paste-and-step IPython script (no function wrapping); per-message helpers live in `loop_helpers.py`. Swap `simulated_message_stream` for a `pika` consumer to flip to production.

## Minor in-memory model cleanup (out of scope, noted for later)

- `StanceEntry.origin_event_id` (`models.py:331`) — drop from the dataclass when the streaming code is migrated to the DB-backed repo.
