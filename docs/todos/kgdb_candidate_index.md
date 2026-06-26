# TODO — kgdb-backed candidate retrieval (durable CandidateIndex + record store)

**Status:** **implemented (approach B — column reconstruction)** + validated on dev
**Area:** `src/entities/linking/index.py`, `kgdb_retrieval.py`, `link.py`, `strategy.py`,
`persistence.py`; `src/listener.py`

> **Done.** `index.py` now defines both halves of the contract (`CandidateIndex` +
> `RecordStore`, with `lookup_candidates(strategy, prep)` the single retrieval call);
> `strategy.retrieval_criteria(prep)` is the column-reconstruction projection of `lookup_keys`;
> `kgdb_retrieval.py` provides `KgdbCandidateIndex` (one SQL query over
> `entities`/`event_properties`/`entity_locations`; `register` is a no-op) and `KgdbRecordStore`
> (reads `entities.metadata`). The listener wires both on a **separate autocommit read
> connection** (writes still go through `KgdbWriter` per record). **Validated:** two separate
> `--once` processes over the same docs — the second **merges** into the first's events (5 → 5,
> no duplicates), vs. the in-memory path which would have produced 10. The in-memory path is
> unchanged (same `lookup`/dict projection). Fixed an inverted-window bug (`date_start > date_end`)
> in both the lookup (`LEAST`/`GREATEST`) and the writer (swap guard). Remaining nuances under
> *Open questions* (grid as a stored column, SQL recency cap, the multi-worker race → reconciliation).
**Related:** [`kgdb_event_persistence.md`](kgdb_event_persistence.md) (Streaming consumer — this
is its named correctness blocker), [`../linking.md`](../linking.md),
[`../../../../media-backend-paid/docs/DATABASE_POSTGRES.md`](../../../../media-backend-paid/docs/DATABASE_POSTGRES.md)

## Why

The streaming listener (`src/listener.py`) dedups only within **one worker's lifetime**: the
linker matches incoming records against an **in-memory** `CandidateIndex`
(`InMemoryCandidateIndex`) plus an in-memory `events` dict, both of which start empty on every
process start. An event that lives **only in kgdb** (a prior worker, a restart, a parallel
worker, or the batch `persist_linked.py`) is invisible — so the listener mints a **duplicate**
instead of merging/updating. Making retrieval read from kgdb closes this.

## The full contract a backend must satisfy (today, only partly defined)

`EntityLinker._process` (`link.py:167`) is the whole dance:

```
prep, drop      = strategy.prepare(record)              # geocode + window + keys
candidate_ids   = index.lookup(strategy.lookup_keys(prep))      # (A) CandidateIndex
match_id, ...   = strategy.adjudicate(prep, candidate_ids, events)   # (B) record store: id→record
if match_id:  base = events[match_id]; strategy.merge(base, prep, index)   # mutate + (A) re-register
else:         new_id, linked = strategy.create(prep, index); events[new_id] = linked
```

So a swappable backend is **two** collaborators, not one:

- **(A) `CandidateIndex`** — `register(key, id)` / `lookup(keys) → set[id]`. **Defined** in
  `index.py`; keys are opaque tuples built by the strategy (`lookup_keys`, `_register` at
  `strategy.py:422`). In-memory impl exists.
- **(B) record store (`events`)** — id→record: `__getitem__` (adjudicate/merge), `__contains__`
  (`match_id in events`), `__setitem__` (create). **Not defined as a protocol** — it's a bare
  `dict` on `EntityLinker`. This is the missing half of the contract.

## Decision 1 — how the DB index retrieves (the unreconciled fork)

| | **A. Opaque-key table** | **B. Column reconstruction** |
|---|---|---|
| Mechanism | new `candidate_index(key text, link_id text)` table; `register`=INSERT, `lookup`=`SELECT … WHERE key = ANY` | `lookup` = SQL over `event_properties` (date `&&`) ∧ `entity_locations` (level ids / grid) ∧ `entity_types` (supertype); no `register` |
| Pros | strategy untouched; keys stay opaque (honors `index.py`’s stated intent) | no redundant table; the writer’s existing rows *are* the index (the intent in `DATABASE_POSTGRES.md`); no register step → no transactionality problem |
| Cons | needs a `register` write transactional with the entity write (Decision 3); duplicates retrieval data | re-implements the strategy’s key logic in SQL; couples the index to the geo/date schema (breaks the “dumb index” abstraction) |

**Recommendation: B**, because the writer *already* persists exactly the retrieval dimensions —
`event_properties.date_start/date_end` (the TODO even says "so a `tstzrange &&` index reproduces
the candidate date filter"), `entity_locations.level_N_id`/coords, `entity_types.entity_type_id`.
`register` becomes a no-op (the writer’s inserts are the registration) and `lookup` is one SQL
query. Cost: the strategy’s key construction (`lookup_keys`) must be expressed as SQL predicates
— so we add a strategy method that yields *structured* retrieval criteria (supertype, geo
id-set + grid cells, date window) instead of opaque key tuples, and the DB index turns those
into SQL. The in-memory index keeps working via the existing opaque keys (both paths derive from
the same criteria).

## Decision 2 — the record store

`events[id]` resolves to the linked record, which is already stored verbatim in
`entities.metadata` (keyed by `metadata->>'_link_id'`). So the DB record store is a read-through
of `entities.metadata`; writes continue via `KgdbWriter.upsert_linked` (unchanged). Add a
`RecordStore` protocol (get / contains / put) so `EntityLinker` depends on it, not on `dict`;
`InMemoryRecordStore` wraps today’s dict.

## Decision 3 — transactionality / write ordering

With **B**, registration disappears, so the only writes are `upsert_linked` (already one
transaction per record). The listener stays: `link_one` (read-only against kgdb for candidate
lookup + record resolution) → `upsert_linked` (the single write). A merge re-reads the candidate
from kgdb, mutates it, and `upsert_linked` persists it. No mid-link writes, no index/entity skew.
(With **A**, registration must move into the `upsert_linked` transaction — extra coupling, the
main reason to prefer B.)

## Plan (assuming B)

1. **Define the contract** in `index.py`: keep `CandidateIndex`, add a `RecordStore` protocol;
   document both as the swappable pair. Refactor `EntityLinker` to hold a `RecordStore` (default
   `InMemoryRecordStore`) instead of a bare dict.
2. **Strategy retrieval criteria**: add `strategy.retrieval_criteria(prep)` returning a structured
   `{supertype, geo_ids, grid_cells, date_window}` object; `lookup_keys` becomes the in-memory
   projection of it, the DB index the SQL projection. Keep behavior identical on the in-memory path
   (regression via `run_linking.py` fixtures).
3. **kgdb index + record store**: `KgdbCandidateIndex.lookup(criteria)` (SQL join over
   `event_properties`/`entity_locations`/`entity_types`, returning `link_id`s) and
   `KgdbRecordStore` (read `entities.metadata` by `_link_id`; `put` is a no-op — the writer owns
   writes). Share the `KGDB_*` connection with `KgdbWriter`.
4. **Wire into the listener**: linker uses the kgdb index + record store (read path), `KgdbWriter`
   the write path — one connection. A worker now dedups against everything in kgdb.
5. **Verify**: restart-across-merge test — write event via listener, restart, publish a second
   document for the same event → expect **update (2 sources)**, not a duplicate. Plus a
   regression run of `run_linking.py` fixtures (in-memory path unchanged) and a parity check that
   the SQL lookup returns the same candidate set as the in-memory keys on a known fixture.

## Open questions

- Grid-cell SQL: store the coordinate grid cell as a column on `entity_locations` (cheap exact
  match) vs. a `point <-> point` distance / PostGIS query. Cheapest: precompute the cell id like
  the in-memory path and index it.
- `candidate_cap` / recency ordering in SQL (`ORDER BY event_properties.date_* DESC LIMIT`).
- Concurrency: two workers creating the "same" event between one's lookup and write — needs the
  canonical↔canonical reconciliation pass ([`canonical_reconciliation.md`](canonical_reconciliation.md))
  as the backstop; the index alone narrows but doesn't eliminate the race.
