# Soft-retire + document-time clock — design and changelog

This document describes two related changes to the tags subsystem
(`src/entities/tags/`) made together:

1. **Soft-retire.** Retention no longer hard-deletes anything. Stance
   entries that are no longer current are flagged retired on disk;
   stance assignments are kept forever.
2. **Document-time stream clock.** Every retention check, consistency
   window, and audit stamp uses the *document's* date (the latest
   `created_at` across the current bundle) instead of wall-clock
   `now()`. Replaying an old corpus behaves as if it were being
   streamed live at that time.

Both changes are scoped to the userdb-backed path
(`StanceCatalogRepo` / `ClaimCatalogStoreRepo` / `EntityStateRepo`
and their callers in `loop_helpers.py` / `stream.py`). The in-memory
`StanceCatalog` in `catalogs.py` already had a `retired_entries` dict
and is unchanged.

---

## 1. Soft-retire

### Motivation

The previous retention rules — set in
`media-backend-paid/docs/social_tags_schema_update_userdb.sql` — were
two `DELETE` statements run at the start of every consistency pass:

```sql
DELETE FROM stance_assignments
 WHERE assigned_at < now() - (:ttl || ' days')::interval;

DELETE FROM stance_entries
 WHERE NOT EXISTS (SELECT 1 FROM stance_assignments
                    WHERE stance_id = stance_entries.stance_id);
```

That destroys history: a single TTL sweep can erase the entire
catalogue, especially when replaying a backfill corpus whose
`assigned_at` is already older than the TTL the moment it's written.

### Rule under soft-retire

- An entry is **retired** when its most-recent `stance_assignments`
  row is older than `assignment_ttl_days`. Retirement is recorded by
  stamping `stance_entries.retired_at` (timestamptz). The row stays
  on disk; assignment rows referencing it stay on disk too. The
  active catalogue (the set the LLM sees in prompts, the set
  surfaced by `iter_entries` / `snapshot`) filters `retired_at IS NULL`.
- Assignments are **never** retention-deleted. `expire_old_assignments`
  was removed.
- The retire trigger fires in three places — all inside the
  consistency pass:
  1. `retire_stale_entries(ttl)` at the top of the pass (TTL rule).
  2. Stage 1 of the pass (`_stage1_deterministic_retire`) — soft-retires
     any active entry with zero assignments at all. Usually a no-op
     once step 1 has run, since zero-assignment entries fail the TTL
     check too.
  3. Stage 3 hygiene `merge(src, dst)` — soft-retires the src entry
     after moving its assignments and aliases onto the dst.

The streaming path itself (`handle_message`) never retires.

### Schema

`stance_entries` gains `retired_at timestamptz NULL`. The original
unique constraint `stance_entries_scope_label_uniq` on
`(entity_id, org_id, primary_type, label)` is dropped and replaced by
a partial index that only enforces uniqueness across active rows, so a
future bootstrap re-add of a previously-retired label can land a fresh
active row alongside the historical one:

```sql
ALTER TABLE public.stance_entries
    ADD COLUMN IF NOT EXISTS retired_at timestamp with time zone NULL;

ALTER TABLE public.stance_entries
    DROP CONSTRAINT IF EXISTS stance_entries_scope_label_uniq;

CREATE UNIQUE INDEX stance_entries_scope_label_active_uniq
    ON public.stance_entries (entity_id, org_id, primary_type, label)
    WHERE retired_at IS NULL;

CREATE INDEX idx_stance_entries_scope_active
    ON public.stance_entries (entity_id, org_id, primary_type)
    WHERE retired_at IS NULL;
```

Standalone migration:
`media-backend-paid/docs/social_tags_soft_retire_userdb.sql`. Same
DDL is folded into `media-backend-paid/db/user_db/schema.sql`.

### Behaviour change matrix

| Operation | Old | New |
|---|---|---|
| `add(label, …)` | `INSERT … ON CONFLICT (entity_id, org_id, primary_type, label) DO NOTHING` then re-fetch by `(entity_id, org_id, primary_type, label)` | `INSERT … ON CONFLICT DO NOTHING` (matches PK collisions and the partial unique on active rows) then re-fetch with `… AND retired_at IS NULL`. Label collision with a *retired* row is allowed and inserts a new active row. |
| `rename(stance_id, new_label, …)` | Look up entry; collision check across all entries | Look up *active* entry only; collision check restricted to `retired_at IS NULL` so a rename onto a retired label is permitted. |
| `merge(src, dst)` | `UPDATE stance_assignments SET stance_id=:dst …; UPDATE stance_entries SET aliases=… WHERE stance_id=:dst; DELETE FROM stance_entries WHERE stance_id=:src` | Same first two steps, then `UPDATE stance_entries SET retired_at = :stream_now WHERE stance_id=:src`. Refuses to operate on already-retired rows. |
| `retire(stance_id)` / `delete(stance_id)` | Guarded `DELETE` (succeeded only when zero assignments) | `UPDATE stance_entries SET retired_at = :stream_now WHERE stance_id=:id AND retired_at IS NULL`. Idempotent. |
| `expire_old_assignments(ttl)` | `DELETE FROM stance_assignments WHERE assigned_at < now() - ttl` | **Removed.** Assignments are preserved. |
| `gc_orphan_entries()` | `DELETE FROM stance_entries WHERE NOT EXISTS (assignments)` | **Removed.** Replaced by `retire_stale_entries`. |
| `retire_stale_entries(ttl)` (new) | — | `UPDATE stance_entries SET retired_at = :stream_now WHERE retired_at IS NULL AND NOT EXISTS (assignment with assigned_at >= stream_now − ttl)` |
| `iter_entries`, `snapshot`, `iter_zero_assignment_entries`, `get_entries_by_ids`, `_primary_type_of` | No active filter | All filter `retired_at IS NULL`. |
| `retired_entries` property | Hard-coded `{}` | Real query: `SELECT * FROM stance_entries WHERE retired_at IS NOT NULL`. |

### `assign()` validation against retired entries

`assign()` calls `_primary_type_of(stance_id)`, which now filters
`retired_at IS NULL`. Consequences:

- The streaming tagger only sees active entries via `snapshot`, so it
  won't normally pick a retired id.
- Redeliveries (RabbitMQ at-least-once) of a message whose target
  entry has been retired in the meantime are *dropped* — the previous
  assignment row (if any) is preserved untouched.

---

## 2. Document-time stream clock

### Motivation

After the soft-retire change, retention SQL still used `now()` —
wall-clock UTC on the database server. That breaks two scenarios:

- **Backfill replay** of a 2025 corpus on a 2026 machine: every
  newly-inserted assignment is already past TTL by wall-clock, so the
  first pass retires everything that was just written.
- **Delayed crawl** in production: bundles flowing in with a
  `created_at` of two days ago compare against wall-clock today,
  so retention is too aggressive relative to the documents' own
  timeline.

The fix: every comparison and audit stamp uses the **stream clock** —
the latest `created_at` across the bundle currently being processed.
Wall-clock UTC is the fallback only when no bundle has been seen yet.

### Concept

`StanceCatalogRepo._stream_now: Optional[datetime]` is set:

- per bundle, by `set_bundle_context(bundle, …)` → max
  `created_at` across `bundle.all_items` (via
  `_latest_created_at`);
- on bootstrap, by `set_items_context(items_by_id, …)` → max
  `created_at` across the whole loaded corpus;
- at startup, by `build_repos` → max
  `stance_assignments.assigned_at` already on disk, so the startup
  retire runs at the stream's last-known position rather than today;
- explicitly, by `set_stream_now(dt)` for tests or unusual callers.

`effective_now()` returns `_stream_now or datetime.now(timezone.utc)`.
**No SQL inside the tags repos references `now()` directly.** Every
comparison or stamp uses `effective_now()` (parameterized as a
`%s::timestamptz` placeholder) or accepts an explicit `stream_now`
kwarg from the caller.

`clear_bundle_context()` does **not** clear the stream clock — that
way a consistency pass running between bundles keeps using the last
observed stream time rather than snapping back to wall-clock. Pass
`set_stream_now(None)` to explicitly opt back into wall-clock.

The same plumbing is mirrored on `ClaimCatalogStoreRepo` /
`ClaimCatalogRepo` so `claim_assignments.extracted_at` follows the
document timeline too.

### Where the stream clock is consulted

| Site | Use |
|---|---|
| `retire_stale_entries(ttl)` | both the TTL comparison (`assigned_at >= stream_now − ttl`) and the `retired_at` stamp |
| `recent_bundle_assignments(max_age_days)` | the `HAVING MAX(assigned_at) >= stream_now − max_age_days` clause |
| `retire(stance_id)` / `merge(src, dst)` | the `retired_at` stamp on the affected rows |
| `assign(StanceAssignment)` | safety-net default for `assigned_at` when the dataclass field is empty |
| `ClaimCatalogRepo._assigned_at_for(...)` | safety-net default for `extracted_at` (still controlled by the `simulate_assigned_at_from_document` flag for the doc-date stamp itself) |
| `EntityStateRepo.mark_consistency_pass(stream_now=…)` | the `last_consistency_pass_at` stamp |
| `EntityStateRepo.mark_bootstrap_complete(stream_now=…)` | the `bootstrap_completed_at` stamp |
| `consistency_pass_due(customer, …, stance_repo=…)` | the `customer.consistency_pass_due(now)` time check |

`bump_streaming` doesn't touch any timestamp and is unchanged.

### Startup anchor

`build_repos` queries
`SELECT MAX(assigned_at) FROM stance_assignments WHERE entity_id=…
AND org_id=…` (helper `_latest_assigned_at` in `loop_helpers.py`),
parses it to an aware datetime, and calls
`stance_repo.set_stream_now(...)` before the startup retire. First-ever
run with no rows returns `None` and the repo falls back to wall-clock
until the first bundle arrives.

---

## 3. File-by-file changelog

### `src/entities/tags/db.py`

`StanceCatalogRepo`:

- New attribute `self._stream_now: Optional[datetime] = None` in
  `__init__`.
- New public methods `set_stream_now(dt)` and `effective_now()`.
- `set_bundle_context(bundle, query_id)` now also calls
  `_latest_created_at(bundle.all_items)` to advance the stream clock.
- `set_items_context(items_by_id, query_id)` same.
- `clear_bundle_context()` preserves the stream clock by design
  (commented).
- `add(...)`: `ON CONFLICT DO NOTHING` (bare), retire-aware re-fetch.
- `rename(...)`: collision check and source lookup now filter
  `retired_at IS NULL`.
- `merge(src, dst)`: refuses to operate on already-retired rows;
  replaces the final `DELETE FROM stance_entries WHERE stance_id=:src`
  with `UPDATE stance_entries SET retired_at = effective_now() WHERE
  stance_id=:src`.
- `retire(stance_id)`: now `UPDATE … SET retired_at = effective_now()
  WHERE … AND retired_at IS NULL`. `delete = retire` alias kept.
- `expire_old_assignments(ttl)`: removed.
- `gc_orphan_entries()`: removed.
- `retire_stale_entries(ttl_days)`: new. Parameterised by
  `effective_now()`; soft-retires entries whose newest assignment is
  older than TTL.
- `iter_entries`, `iter_zero_assignment_entries`, `get_entries_by_ids`,
  `_primary_type_of`: all filter `retired_at IS NULL`.
- `retired_entries` property: real query against
  `retired_at IS NOT NULL`.
- `assign(...)`: fallback for empty `assigned_at` is
  `self.effective_now().isoformat()` instead of `now_iso()`.
- `recent_bundle_assignments(...)`: `HAVING` clause uses
  `effective_now()` as the upper anchor of the recency window.

`ClaimCatalogRepo`:

- New `_stream_now: Optional[datetime]` and `effective_now()`.
- `set_bundle_context(items_by_id, query_id, stream_now=None)`:
  extended signature carries the clock.
- `_assigned_at_for(source_item_id)`: returns
  `self.effective_now().isoformat()` instead of `now_iso()` for the
  non-simulate fallback.

`ClaimCatalogStoreRepo`:

- New `_stream_now`; `set_bundle_context(bundle, query_id)` derives it
  via `_latest_created_at`. `clear_bundle_context` keeps it.
- `_build_repo(event_id)` forwards `_stream_now` to each
  `ClaimCatalogRepo` it constructs.

`EntityStateRepo`:

- `mark_consistency_pass(entity_id, org_id, *, stream_now=None)`:
  stamp is parameterised; falls back to UTC `now()` when no
  `stream_now` is passed.
- `mark_bootstrap_complete(entity_id, org_id, *, stream_now=None)`:
  same.

Module-level helpers (new):

- `_parse_iso_datetime(value)` — defensive ISO parser; coerces naive
  datetimes to UTC.
- `_latest_created_at(items)` — max `created_at` across a set of
  `SourceItem`s.

### `src/entities/tags/loop_helpers.py`

- `build_repos`:
  - After `state_repo.ensure(...)`, queries `_latest_assigned_at` and
    calls `stance_repo.set_stream_now(...)` so the startup retire is
    anchored at the stream's last-known position.
  - Calls `stance_repo.retire_stale_entries(ttl)` once.
  - Logs both the anchor and the retire count.
- `_latest_assigned_at(conn, entity_id, org_id)`: new helper, returns
  `Optional[datetime]`.
- `consistency_pass_due(customer, bundles_processed,
  consistency_every_n_bundles, stance_repo=None)`: new optional
  `stance_repo` parameter; the time-threshold branch uses
  `stance_repo.effective_now()` when supplied, else wall-clock UTC.
- `run_consistency_pass`:
  - Two-step retention pair replaced with a single
    `stance_repo.retire_stale_entries(ttl)` call.
  - `state_repo.mark_consistency_pass(...)` now passes
    `stream_now=stance_repo.effective_now()`.

### `src/entities/tags/stream.py`

- `state_repo.mark_bootstrap_complete(...)` now passes
  `stream_now=stance_repo.effective_now()`.
- Main-loop call site:
  `consistency_pass_due(customer, i, CONSISTENCY_EVERY_N_BUNDLES,
  stance_repo)`.

### `media-backend-paid/db/user_db/schema.sql`

- `stance_entries` table: new column `retired_at timestamp with time
  zone`.
- Constraint `stance_entries_scope_label_uniq` removed.
- New partial unique index `stance_entries_scope_label_active_uniq`
  on `(entity_id, org_id, primary_type, label) WHERE retired_at IS
  NULL`.
- New partial index `idx_stance_entries_scope_active` on
  `(entity_id, org_id, primary_type) WHERE retired_at IS NULL`.

### `media-backend-paid/docs/social_tags_soft_retire_userdb.sql`

New standalone migration. Idempotent. Adds the column, drops the
constraint, creates the partial indexes. Run by hand against any
environment whose `schema.sql` hasn't been re-reflected.

### `src/entities/tags/serialization_plan.md`

- "Stance retention" bullet rewritten to describe soft-retire and
  preserved assignment history.
- `stance_entries` table row gains the `retired_at` column and the
  partial unique index description.
- Mutation cascade rules table updated: `merge` ends in `UPDATE
  retired_at`, `retire`/`delete` is a guarded soft `UPDATE`.
- "Retention (stances only)" SQL block replaced with the single
  `UPDATE … SET retired_at = …` statement.

### `src/entities/tags/readme_tags.md`

- DB-mapping row for `stance_entries` describes soft-retire and the
  preservation invariant.
- Mutation interface table: `merge` and `retire` rows updated.
- Query-interface table: `iter_entries`, `iter_zero_assignment_entries`,
  and `get_entries_by_ids` annotated with the `retired_at IS NULL`
  filter.
- SQL-mapping list under "How the DB switch looks in code": `add` /
  `assign` / `merge` / `retire` rewritten.
- Retention SQL block replaced with the soft-retire `UPDATE`.

---

## 4. Migration and rollout

1. Apply `media-backend-paid/docs/social_tags_soft_retire_userdb.sql`
   against userdb. Idempotent — safe to re-run.
2. Re-deploy the tags code. Existing rows stay active (the new
   column defaults to `NULL`).
3. On the next run, `build_repos` performs the startup retire using
   the stream clock anchored at the latest existing `assigned_at`.
4. Subsequent consistency passes use the per-bundle stream clock for
   retention checks and audit stamps.

There is no down-migration path that recovers a hard-deleted row, but
nothing in the new code path deletes anything to begin with.

---

## 5. Verifying the behaviour

Quick spot-checks in IPython after running `stream.py` against an old
corpus:

```python
# 1. All assignment rows are preserved regardless of age.
cur = conn.cursor()
cur.execute("SELECT COUNT(*), MIN(assigned_at), MAX(assigned_at) "
            "FROM stance_assignments WHERE entity_id=%s AND org_id=%s",
            (customer.entity_id, ORG_ID))
print(cur.fetchone())

# 2. The active catalogue only contains entries with a recent assignment
# in *stream time*.
cur.execute("""
    SELECT label, retired_at,
           (SELECT MAX(assigned_at) FROM stance_assignments a
             WHERE a.stance_id = e.stance_id)
      FROM stance_entries e
     WHERE entity_id=%s AND org_id=%s
     ORDER BY retired_at NULLS FIRST, label
""", (customer.entity_id, ORG_ID))
for row in cur.fetchall():
    print(row)

# 3. Stream clock is anchored at the latest doc time, not wall-clock.
print("effective_now =", stance_repo.effective_now())
```

For a backfill corpus dated 2026-05-22, the consistency pass should
report retentions stamped with `retired_at` somewhere on
2026-05-22 — not today's wall-clock date — and the assignment count
should not drop after retention.
