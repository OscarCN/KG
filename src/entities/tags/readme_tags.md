# Tags subsystem

The tags subsystem extracts and curates two kinds of structured signal
from news and social-media posts, for a single **customer** (an entity
in the knowledge graph):

- **Stances** — recurring ideas the public expresses about the customer
  (complaints, suggestions, denuncias, gratefulness, …). Stances live
  in a per-customer **stance catalog** that grows over time.
- **Claims** — factual statements made about the customer inside a
  specific event. Claims live in a per-(customer, event) **claim
  catalog**.

Both catalogs are repositories: a fixed schema with append-only
assignment tables. The streaming pipeline mutates them as new posts
arrive; a periodic consistency pass curates them.

The whole system was designed in-memory first, but every shape and
query is built to map cleanly to a relational database. See
[§ DB mapping](#db-mapping).

---

## Core vocabulary

| Term | Definition |
|---|---|
| **Customer** | An entity in the KG (`kgdb.entities`). The pipeline runs **per customer**; everything below scopes to one customer. |
| **Source item** | A post, news article, or user comment. Three kinds: `article`, `user_post`, `user_comment`. |
| **Bundle** | One root post/article **plus its comments**. The streaming unit. **One bundle = one root source item**; comments are not bundles. |
| **Event** | A KG event (`kgdb.events`). Bundles can be linked to 0..N events. Stances are event-independent; claims are event-scoped. |
| **Stance** | A recurring idea expressed about the customer (`label` + `description`). Stored as `StanceEntry` in the per-customer catalog. |
| **Stance type** | One of 9 categories: `entity_stance`, `complaint`, `gratefulness`, `suggestion`, `request`, `denuncia`, `question`, `endorsement`, `noise`. The first 8 are **stance-bearing** (live in the catalog); `noise` is tag-only (assignment-only). |
| **Assignment** | A row that ties one source item to one catalog entry (`StanceAssignment` for stances, `ClaimAssignment` for claims). The interesting derivative is `stance_id=null` rows — "we saw this, it was a `<type>`, no entry fit." |
| **Triage hint** | The per-item, per-stance-type classifier output. Catalog-free. One bundle yields N triage rows (multi-stance per item). |
| **Claim** | A factual statement about the customer inside an event (verbatim quote + importance). Grouped into clusters under a canonical phrase. |

---

## Pipeline overview

```
                       ArticleBundleRetriever
                              │
                              ▼
                       ┌─────────────┐
                       │   Bundle    │  root + comments + event_ids
                       └──────┬──────┘
                              │
              ┌───────────────┴─────────────────┐
              │                                 │
              ▼                                 ▼
        Bootstrap (once)              Streaming (per bundle)
              │                                 │
              ▼                                 ▼
       Seed StanceCatalog             Triage → tag → update
                                      (+ claim extract/group
                                         when event linked)
                                                │
                                                ▼
                                      Consistency pass (periodic)
                                      • Stage 1: deterministic retire
                                      • Stage 2: orphan bootstrap
                                      • Stage 3: hygiene (merge/rename)
```

Three execution phases. They share state through the in-memory
`StanceCatalog`, `ClaimCatalogStore`, and `Customer` counters, all of
which are serializable.

### Phase 1 — Bootstrap (one-shot)

**Code:** `bootstrap.py`. **Prompt:** `prompts/bootstrap_per_type.txt`.

Seeds the per-customer stance catalog from a corpus of bundles. For
each bundle in the seed window:
1. Triage every item (`TypeTriageStep` — same prompt as streaming).
2. Group triage rows by `stance_type`, drop `noise`.
3. Per active type, **one LLM call** clusters the rows into 0..30
   stance entries (`bootstrap_per_type.txt`). Each entry requires
   ≥ `min_evidence` distinct source_item_ids.
4. For each entry, write `StanceAssignment` rows linking the
   source_item_ids the LLM grouped under it (catalogued assignments,
   `stance_id=entry.id`).
5. For every triaged source_item that was **not** placed in any
   cluster of this type (below `min_evidence`, trimmed, or just
   un-clustered), synthesize a `stance_id=null` assignment carrying
   the triage hint's `brief_summary` as `reason`.

After bootstrap, the catalog has entries **and** assignments —
behaviorally equivalent to a streaming-tagged pass over the same
bundles. The streaming loop then **skips** the seed window
(`bundles[BOOTSTRAP_BUNDLE_LIMIT:]`), avoiding double work.

### Phase 2 — Streaming (per bundle)

**Code:** `streaming.py`. **Prompts:** `triage.txt`, `tag_per_type.txt`,
`claim_extract.txt`, `claim_group.txt`.

`StreamingTagsPipeline.process_bundle(bundle)` per bundle:

1. **Remember items** — store every item in `state.items_seen` (id → SourceItem).
2. **Triage** — one LLM call classifies every item in the bundle into
   0..N typed rows. Catalog-free; the model decides which
   stance_type fits each idea.
3. **Stance tag per type** — for each stance-bearing type with any
   triage hints, ONE LLM call routes those hints to existing catalog
   entries (or proposes new ones, or marks them `stance_id=null`).
4. **Stance update** — deterministic apply. New `add` proposals
   create entries (with ≥2 distinct evidence ids); `rename` proposals
   update labels; assignments are appended. No adjudicator LLM.
5. **Claim extract + group** — when the bundle has linked events, for
   each linked event: one LLM call extracts factual claims from the
   root post (optionally including comments), then one LLM call routes
   each claim into an existing event-scoped cluster (or creates a new
   one, or drops it).
6. **Counter bookkeeping** — increment `customer.bundles_processed_total`
   and `customer.bundles_processed_since_last_pass`. One increment per
   bundle; the consistency pass uses
   `bundles_processed_since_last_pass` to size its window and to decide
   whether the pass is due (via
   `Customer.consistency_pass_due` against
   `consistency_pass_threshold_bundles`).

The streaming pipeline does **not** edit existing entries' labels or
merge entries — that's the consistency pass's job.

### Phase 3 — Consistency pass (periodic)

**Code:** `consistency.py`. **Prompts:**
`prompts/bootstrap_per_type.txt` (reused by Stage 2),
`prompts/hygiene_per_type.txt` (Stage 3).

Triggered mid-stream or end-of-run. Three stages per active stance
type, sized by a **bundle window**:

```
K = ceil(customer.bundles_processed_since_last_pass * 1.25)
window = catalog.recent_bundle_assignments(
    n_bundles=K,
    kinds=("article","user_post"),
    max_age_days=3,   # cutoff: drop bundles older than this even if K isn't filled
)
```

The window is the K most-recent unique post/article source_item_ids
(comments excluded for now) ranked by `max(assigned_at)`, additionally
restricted to bundles whose most-recent assignment is within
`max_age_days` of now (default 3d — tighter than the assignment TTL,
so the LLM stages only see what's actually current). The call returns
every assignment (all kinds, all stance_ids including null) for that
source-id set; the result is **at most** K bundles and may be fewer if
the age cutoff bites. Stages 2 and 3 each operate on
`window_per_type`.

- **Stage 1 — Deterministic retire (no LLM).** For each entry,
  count `assignments` over **all time** (not just the window). Any
  entry with zero catalogued assignments is moved to
  `catalog.retired_entries`. Cheap; eliminates dead entries before
  any LLM stage runs.

- **Stage 2 — Orphan bootstrap (one LLM call per type, when triggered).**
  Collect null-stance assignments in the window. If `< min_evidence`,
  skip. Otherwise rebuild `TypeTriageItem`s from those rows (the
  assignment carries `source_item_id`, `source_kind`, and
  `brief_summary` via `reason`; `text` is fetched from `items_seen`),
  and call `BootstrapStep._bootstrap_one_type` — the same code path
  Phase 1 uses. For each returned cluster:
  - De-dup against an existing entry of the same type with a matching
    normalized label.
  - Otherwise add a fresh entry.
  - Route the matching null-stance assignments to the new (or matched)
    entry **in place** (`_route_nulls_to_entry`) — mutating `stance_id`
    on the existing rows, preserving `assigned_at`. No new rows are
    created, so there are no duplicates.

- **Stage 3 — Hygiene (one LLM call per type).** Operates on the
  catalog snapshot, including entries Stage 2 just minted. The prompt
  sees, per entry:
  - `id` (short `st_N`), `label`, `description`.
  - `n` — total catalogued assignments **of this type** (all-time, not
    window-restricted).
  - `samples` — up to 5 `{text, reason}` pairs drawn from the window.
  No full items array, no full assignments array. The LLM emits only
  `merges` and `rename`:
  - `merges`: an N-way list of `ids` (≥ 2 short ids); the last id in
    the list survives, others are absorbed into it via
    `catalog.merge`. Optional `new_label` / `new_description`
    re-render the surviving entry after the merge, so the merged
    postura can be generalized.
  - `rename`: rename a single existing entry's label/description.
  No `add`, `retire`, or `reroute` — Stage 1 handles retires
  deterministically, Stage 2 handles adds via bootstrap, and reroutes
  were dropped entirely (see "Why no splits / reroutes" below).

After all stages run, `customer.bundles_processed_since_last_pass` is
reset to 0 and `customer.last_consistency_pass_at` is stamped.

#### Why no splits / reroutes

Splitting an over-broad stance into 2+ would orphan every previously
tagged item until they could be re-routed — a window that's
unappealing. Reroutes were dropped for the same reason: any stance
move requires LLM-judged routing of historical items, which is
token-heavy and risky. The current strategy:

- Renames let a too-broad stance be **narrowed in place** (Stage 3).
- Stage 2's orphan bootstrap mints **complementary** entries from the
  null-stance pool — naturally creating the "other half" of what a
  split would have produced.
- Mis-routed historical items stay until they age out or are fixed
  manually.

Splits are a future TODO; the proposed flow is: select entries with
unusually high `n`, send each to a per-stance splitting LLM call that
re-tags every assigned item into 2+ sub-stances. See
[§ Open work](#open-work).

---

## Catalogs as data — the repository view

The catalogs are the durable state of the system. Two repositories,
both currently in-memory and JSON-serialized but built so the same
shape maps to a relational database.

### StanceCatalog (per customer)

```
StanceCatalog(customer_id)
├── entries          : dict[entry_id, StanceEntry]      ← active
├── retired_entries  : dict[entry_id, StanceEntry]      ← retired (history)
└── assignments      : list[StanceAssignment]           ← append-only
```

**StanceEntry** is the curated row:
`id`, `label`, `description`, `primary_type`, `created_at`,
`aliases` (history of past labels after renames/merges),
`origin_event_id`.

**StanceAssignment** is one row per (item × type) attempt:
`source_item_id`, `source_kind`, `customer_id`, `stance_id`
(nullable), `stance_type`, `event_id` (nullable), `reason`,
`assigned_at`.

Crucial property: **every triaged item produces an assignment**, even
when no entry fits. The `stance_id=null` row carries the triage hint's
`brief_summary` as `reason`. This is what makes Stage 2 (orphan
bootstrap) possible — null rows are the pool to cluster.

### ClaimCatalog (per customer × event)

```
ClaimCatalogStore
└── catalogs : dict[(customer_id, event_id), ClaimCatalog]
              ├── clusters    : dict[cluster_id, ClaimCluster]
              └── assignments : list[ClaimAssignment]
```

**ClaimCluster** is a canonical phrase + its member `RawClaim`s. A
cluster has `freshness_window_hours` — old clusters can be aged out by
the streaming pipeline if they go stale.

### Mutation interface (the only writers)

These methods are the **complete** mutation surface. No code outside
these methods should write to the catalogs:

| Method | Effect | DB equivalent |
|---|---|---|
| `StanceCatalog.add(label, description, *, primary_type)` | Create entry | `INSERT INTO stance_entries …` |
| `StanceCatalog.assign(assignment)` | Append assignment after validation | `INSERT INTO stance_assignments …` |
| `StanceCatalog.rename(stance_id, new_label, new_description)` | Update entry; old label → `aliases` | `UPDATE stance_entries SET label, description …` |
| `StanceCatalog.merge(src_id, dst_id)` | Move all assignments from src to dst; src label → dst aliases; src removed | `UPDATE stance_assignments SET stance_id=:dst …; DELETE …` |
| `StanceCatalog.retire(stance_id)` | Move entry from `entries` to `retired_entries` | `UPDATE stance_entries SET retired_at=now() …` |
| `StanceCatalog.reroute(from_id, to_id)` | Move all assignments from one stance to another | `UPDATE stance_assignments SET stance_id=:to …` |

### Query interface (the only readers)

Same principle: every read goes through one of these methods so
swapping the in-memory backing for a DB query is a localized change.

| Method | Returns | DB equivalent |
|---|---|---|
| `iter_entries(*, types=None)` | Active entries, optionally filtered by `primary_type` | `SELECT * FROM stance_entries WHERE entity_id=… AND org_id=… [AND primary_type = ANY(:types)]` |
| `assignments_for(*, types, stance_id, event_id)` | Assignments matching the given filters | `SELECT * FROM stance_assignments WHERE …` |
| `count_catalogued_assignments(*, stance_type=None)` | `{stance_id: count}` for non-NULL `stance_id` rows | `SELECT stance_id, COUNT(*) FROM stance_assignments WHERE … GROUP BY stance_id`. Used by consistency Stage 3 in place of a Python count loop. |
| `iter_zero_assignment_entries()` | Entries with no catalogued assignments (Stage 1 retire candidates) | `SELECT * FROM stance_entries e WHERE NOT EXISTS (SELECT 1 FROM stance_assignments WHERE stance_id = e.stance_id)` |
| `get_entries_by_ids(ids)` | `{stance_id: StanceEntry}` for the supplied id set | `SELECT * FROM stance_entries WHERE stance_id = ANY(:ids)`. Used by Stage 3 to prefetch entries referenced by merge proposals. |
| `summary(*, types, event_id, top_n)` | `(label, count)` rows by count desc | `SELECT label, count(*) FROM stance_assignments JOIN stance_entries USING (stance_id) GROUP BY …` |
| `snapshot(*, types)` | Compact prompt-ready entry list | Same as `iter_entries` projected to the prompt fields |
| `recent_bundle_assignments(*, n_bundles, kinds)` | All assignments belonging to the K most-recent post/article bundles | See SQL sketch below |

**Avoid in hot loops.** `StanceCatalogRepo.entries` and
`StanceCatalogRepo.assignments` are `@property` snapshots — each
access issues a full scan. They're fine for one-shot stats / printout
helpers; anything that loops should call the explicit methods above.

`recent_bundle_assignments` is the windowed query used by the
consistency pass. It groups assignments by `source_item_id` among the
given `kinds`, optionally drops groups whose `MAX(assigned_at)` falls
outside `max_age_days`, ranks by `max(assigned_at)` desc, takes the
top K, and returns every assignment (all kinds, all stance_ids) for
that set:

```sql
WITH recent AS (
    SELECT source_item_id, MAX(assigned_at) AS last_at
    FROM stance_assignments
    WHERE source_kind IN (:kinds)
    GROUP BY source_item_id
    HAVING :max_age_days IS NULL
        OR MAX(assigned_at) >= now() - (:max_age_days || ' days')::interval
    ORDER BY last_at DESC
    LIMIT :n_bundles
)
SELECT a.* FROM stance_assignments a
JOIN recent USING (source_item_id);
```

Post/article `source_item_id` is the bundle proxy — one root post or
article per bundle, so K unique post/article source_item_ids = K
bundles.

---

## DB mapping

Each in-memory shape maps cleanly to a relational table in **userdb**,
following the same `(entity_id, org_id, query_id)` convention as
`entities_documents_sentiments_org`. Schema lives in
[`serialization_plan.md`](./serialization_plan.md); the standalone
migration is [`media-backend-paid/docs/social_tags_schema_update_userdb.sql`](../../../media-backend-paid/docs/social_tags_schema_update_userdb.sql)
and the userdb source of truth is `media-backend-paid/db/user_db/schema.sql`
(see [`DATABASE_POSTGRES.md`](../../../media-backend-paid/docs/DATABASE_POSTGRES.md)
§ Tags subsystem).

Tables:

| Table | Role |
|---|---|
| `stance_entries` | Per-`(entity, org)` stance catalog. Hard-deleted when no assignments remain. |
| `stance_assignments` | One row per `(source_item, entity, org, stance_type)`. `stance_id` is nullable — NULL rows are the orphan pool consumed by consistency Stage 2; a unique index over the four columns (across NULL/non-NULL) plus `ON CONFLICT DO UPDATE` keeps re-tagging idempotent and latest-wins. |
| `claim_clusters` | Per-`(entity, org, event_id)` claim cluster. No TTL — clusters persist with their event. |
| `claim_assignments` | One row per extracted `RawClaim` placed into a cluster. Unique on `(source_item_id, entity_id, org_id, event_id, cluster_id, verbatim_hash)` so a redelivered bundle can't duplicate claims (`verbatim_hash` = sha256 of `verbatim`, computed app-side). |
| `tags_entity_state` | Per-`(entity, org)` streaming counters + `assignment_ttl_days` (default 4) + consistency-pass thresholds. |

Scope rules:

- Stances and claims are scoped per `(entity_id, org_id)`. `query_id` is denormalised on each assignment row for traceability — not part of any catalog key.
- `entity_id` references `kgdb.entities_alias.original_entity_id` (cross-DB, app-level only).
- Stance TTL is configurable per `(entity, org)`; default 4 days (range 3–5). Claims have no TTL.

Column conventions (`stance_assignments`, `claim_assignments`):

- **`parent_source_id` is the post-level URL** — parent post URL for `user_comment` rows, the row's own `source_item_id` for root rows (`article` / `user_post`). This **diverges from `entities_documents_sentiments_org.parent_doc_id`** (which is NULL for roots): the tags-side convention exists so per-post aggregations (`GROUP BY parent_source_id`, "all assignments for post X" → `WHERE parent_source_id = :url`) need no `COALESCE` and no `IS NULL` branching. Filled at write time by `StanceCatalogRepo._enrich_assignment_from_context` and `ClaimCatalogRepo._ctx_for`.
- **`news_type` is inherited from the parent post** for comment rows. Comment items only carry comment-level fields in their metadata (`comment_id`, `comment_text`, …), so the enrichment walks one step up to the parent post to pull its network identifier. Root rows read it from their own metadata directly.
- **`query_id` is denormalised attribution**, not catalog key. Filled from the per-message context (streaming) or the bootstrap `query_id` parameter (Phase 1).

### How the DB switch looks in code

[`db.py`](./db.py) implements the repos with the **same** method
surface as `catalogs.py`:

- `StanceCatalogRepo(conn, *, entity_id, org_id)` — drop-in for `StanceCatalog`.
- `ClaimCatalogStoreRepo(conn, *, entity_id, org_id)` and `ClaimCatalogRepo(conn, *, entity_id, org_id, event_id)` — drop-ins for `ClaimCatalogStore` / `ClaimCatalog`.
- `EntityStateRepo(conn)` — counter / threshold / consistency-pass timestamp persistence.
- `connect_userdb()` — psycopg2 connection from `USERDB_*` env vars.

The repos do **not** commit — the caller (the message handler) owns
the transaction lifecycle: one transaction per bundle, commit before
acking the queue message.

SQL mapping:

- `add(…)` → `INSERT INTO stance_entries … ON CONFLICT (entity_id, org_id, primary_type, label) DO NOTHING RETURNING *`
- `assign(a)` → `INSERT INTO stance_assignments … ON CONFLICT (source_item_id, entity_id, org_id, stance_type) DO UPDATE SET stance_id = EXCLUDED.stance_id, reason = …, assigned_at = …, event_id = …` (one row per item/type; latest tagging decision wins).
- `merge(src, dst)` → transaction: `UPDATE stance_assignments SET stance_id=:dst WHERE stance_id=:src`, `UPDATE stance_entries SET aliases = aliases || to_jsonb(:src_label) WHERE stance_id=:dst`, `DELETE FROM stance_entries WHERE stance_id=:src`. FK `ON DELETE RESTRICT` guards the ordering.
- `retire(id)` / `delete(id)` → guarded `DELETE FROM stance_entries WHERE … AND NOT EXISTS (matching assignments)`.
- `recent_bundle_assignments(…)` → the CTE above.

Retention (run at consistency-pass start):
```sql
DELETE FROM stance_assignments
 WHERE entity_id = :e AND org_id = :o
   AND assigned_at < now() - (:ttl || ' days')::interval;

DELETE FROM stance_entries
 WHERE entity_id = :e AND org_id = :o
   AND NOT EXISTS (SELECT 1 FROM stance_assignments
                    WHERE stance_id = stance_entries.stance_id);
```

The streaming pipeline, bootstrap step, and consistency pass don't
need to change — they only ever call the catalog method surface.

### Bundle-context enrichment

`StanceCatalogRepo` and `ClaimCatalogStoreRepo` each expose
`set_bundle_context(bundle, query_id)`. The streaming consumer calls
it once per message; subsequent `assign(...)` / `create(...)` calls
auto-fill the dimensions the tagger doesn't know about
(`parent_source_id` from `item.parent_source_id`, `news_type` from
`item.metadata['news_type']`, `query_id` from the message) by looking
up each assignment's `source_item_id` against the bundle's items.

This is the reason the streaming-side tagger code (`tagging.py`,
`bootstrap.py`) doesn't need to know about org/query/news-type
dimensions: the repo enriches at write time. Non-streaming callers
(tests, scripts, the consistency pass) can ignore the hook entirely —
when context isn't set, `assign()` writes whatever the dataclass
already carries.

### Backfill mode (`simulate_assigned_at_from_document`)

Both `StanceCatalogRepo` and `ClaimCatalogStoreRepo` accept a
constructor flag `simulate_assigned_at_from_document`. When True, the
bundle-context enrichment overwrites the assignment's `assigned_at`
(stance) / `extracted_at` (claim) with the bundle item's `created_at`
— the article's `date_created` for root posts/articles, the parent
post's `date_created` for comments (their own `comment_timestamp` is
often missing on social sources). Off by default; the production live
stream wants wall-clock.

Why it exists: when replaying a static corpus all at once, every row
gets stamped within the same few seconds of wall-clock, so the
consistency-pass age cutoff (`max_age_days`, default 3d) is a no-op.
Backfill mode shifts the timestamps onto the article date axis so the
cutoff actually bites and the consistency-pass window reflects the
simulated stream time. Flip it on with the `SIMULATE_ASSIGNED_AT_FROM_DOCUMENT`
knob at the top of [`stream.py`](./stream.py).

### Streaming entry point

[`stream.py`](./stream.py) is the runtime around the DB-backed repos.
It is **a script, not a library** — top-level code only, paste-and-step
in IPython. The module-level flow:

1. Edit knobs at the top — `ORG_ID`, `QUERY_ID`, `BUNDLE_LIMIT`,
   `CONSISTENCY_EVERY_N_BUNDLES`, fixture paths.
2. `config = LocalRunConfig(...)` — same shape `run_tags.py` uses.
3. `customer = load_customer(...)` and `conn = connect_userdb()`.
4. `stance_repo, claim_store, state_repo = build_repos(conn, customer, ORG_ID)`.
5. `state_repo.apply_counters_to(customer, ORG_ID)` — hydrate the
   in-memory `Customer` from `tags_entity_state` so
   `consistency_pass_due()` reflects what's persisted, not the JSON
   fixture.
6. `pipeline = build_streaming_pipeline(customer, config, state)` and
   `consistency_step = build_consistency_step(customer, config)`.
7. `messages = simulated_message_stream(...)` — generator that yields
   `TagsMessage` per local-fixture bundle. Stays paused between
   bundles so you can `msg = next(messages)` one at a time, or drain
   it via the `for` loop further down to fast-forward.
8. The main loop calls `handle_message(...)` per message: sets the
   per-bundle context on both repos, runs `pipeline.process_bundle`,
   bumps `tags_entity_state` counters, commits. On commit, the
   bundle is durable. Redelivery is idempotent via the unique
   indexes on `stance_assignments` and `claim_assignments`.
9. If `consistency_pass_due(...)` returns True, `run_consistency_pass`
   handles retention → recent-bundle window → ES/local item fetch →
   Stages 1–3 → `mark_consistency_pass` → commit.

The message envelope is `TagsMessage` (`models.py`) carrying
`(bundle, entity_id, org_id, query_id)`. Swapping
`simulated_message_stream` (in `loop_helpers.py`) for a `pika`
consumer that yields the same shape is the only change needed to
flip to the real queue — the main loop doesn't care about the
message origin.

### Source-item text recovery for the consistency pass

`StreamingState.items_seen` is process-local. After a worker restart
it's empty, so the consistency pass needs to rebuild text for the
recent-bundle window from somewhere else. [`source_items.py`](./source_items.py)
provides two `SourceItemFetcher` implementations:

- `LocalFileSourceItemFetcher(linked_path)` — re-reads the same
  `linked.json` the retriever uses; serves all lookups from a flat
  in-memory index keyed by `source_item_id`.
- `ESSourceItemFetcher(index="news", connection_alias="medios3conn")`
  — one `terms` query on `url` per pass against the ES `news` index.
  Comments are embedded on each parent post (`doc.comments[…]`); the
  fetcher flattens them into individual `SourceItem`s keyed by
  comment id.

Both expose `fetch_for_assignments(assignments)` →
`dict[source_item_id, SourceItem]` shaped exactly like the in-memory
`items_seen`. `_run_consistency_pass` in `stream.py` invokes the
fetcher with the recent-bundle window so Stage 2 (orphan bootstrap)
and Stage 3 (hygiene sampling) see actual text, not just the brief
`reason` strings stored on each assignment row.

### State on restart

No load-time reconstruction step exists. The repos are query-backed —
`iter_entries`, `assignments`, `recent_bundle_assignments` each issue
SQL on demand. On worker restart:

```python
stance_repo = StanceCatalogRepo(conn, entity_id=…, org_id=…)
state_repo  = EntityStateRepo(conn)
state_repo.apply_counters_to(customer, org_id)  # hydrate Customer counters
```

That's it — the catalog is "live"; the next `process_bundle` call
issues whatever SELECTs the streaming step needs and the writes flow
through the same `assign()`/`add()` methods.

---

## Prompt patterns

Every prompt in this subsystem follows the same template. The full
style guide is in `prompts/style_guide.md`; this section names the
patterns at a glance.

### Layout

```
<task description, 1–3 short paragraphs naming domain terms>

<objectives — bulleted, written from the *output*'s perspective>

<one-line summary of what to do>


CLIENTE (entidad principal — sólo cuentan …):
{customer}

EVENTO (contexto opcional):
{event}

GUÍA DEL TIPO `{stance_type}` (define la forma …):
{stance_type_guide}

<other injected blocks, each with a role-label>


REGLAS

- <constraint 1>
- <constraint 2>

<field glossary inline — one line per field>


Responde EXCLUSIVAMENTE con JSON:

{<schema>}

Ejemplo (annotation explaining what's non-trivial about this example):

{<example output>}

Si no hay nada accionable, devuelve {<empty wrapper>}.
```

### Recurring patterns

1. **One-shot, JSON-only.** Every prompt returns a single JSON object;
   the LLM is wrapped in `OpenRouterJsonLlm` with
   `response_format={"type":"json_object"}` and retries on parse
   failure. The parser tolerates a leading ```` ```json ```` fence.

2. **Customer-relevance gate.** Every prompt that accepts `{customer}`
   restates: "sólo cuentan posturas/claims que le aplican directa o
   indirectamente". Without it the model classifies everything.

3. **Type guide injection.** Per-type prompts inject
   `prompts/types/<type>.txt` so the model sees the shape and
   abstraction level for that specific stance type. The triage prompt
   concatenates all 9 guides; bootstrap/tag/hygiene per-type prompts
   inject just one.

4. **Short JSON keys, long Python names.** At the LLM boundary the
   prompt uses short keys (`id`, `type`, `summary`, `idx`); the parser
   maps them back to dataclass field names (`source_item_id`,
   `stance_type`, `brief_summary`, `claim_index`). The parser also
   accepts the long keys as fallback so cached responses don't break.

5. **Local integer ids.** Items are renumbered 1..N inside each prompt
   call (`id_map: dict[int, str]`); stance ids are renumbered `st_N`
   (`stance_id_map: dict[str, str]`); cluster ids are renumbered
   `cl_N`. The model never sees long canonical strings — saves tokens
   and gets cleaner JSON. The maps are mutated in place by the prompt
   builder so the parser can reverse them.

6. **Absorption categories OMIT, not EMIT.** Items that don't belong
   (off-topic, greeting, promo, sarcasm) are dropped from the output
   entirely instead of emitting a `kind=noise` row. We recover
   coverage from `items_seen - items_with_rows = absorbed_count`.

7. **`stance_id=null` is meaningful, not absence.** The streaming
   tagger synthesizes a null-stance assignment for every triage hint
   the LLM didn't place. Bootstrap does the same for un-clustered
   hints. This is what makes the null-stance pool a first-class
   signal for Stage 2.

8. **Wrapper key names the content, not the step.** Top-level JSON
   keys are nouns describing each entry (`rows`, `entries`,
   `assignments`, `proposals`, `merges`, `rename`, `claims`,
   `decisions`, `mutations`) — never the step name.

9. **Backward-compat at the parser, not the prompt.** Field renames
   at the LLM boundary keep `raw.get("new_name", raw.get("old_name"))`
   in the parser so cached responses don't poison reruns. The prompt
   shows only the new key.

10. **Each constraint stated once.** No re-stating rules in both the
    REGLAS section and the field glossary. The example demonstrates
    the non-trivial behavior the rules describe (multi-row per item,
    omission, an empty wrapper).

### Mutating id maps

The prompt builders in `prompts.py` follow a uniform pattern: a caller
passes an empty dict; the builder fills it as it walks the input; the
caller hands the same dict to the parser to reverse the mapping. This
keeps the builder and parser symmetric:

```python
triage_id_map: dict[int, str] = {}
stance_id_map: dict[str, str] = {}
prompt = tag_prompt_for_type(customer, items, hints, slice_, stance_type,
                              triage_id_map=triage_id_map,
                              stance_id_map=stance_id_map)
response = llm.call(prompt)
# parser uses triage_id_map[1] → canonical id, stance_id_map["st_3"] → canonical id
```

### Cache layout

```
cache/tags_<phase>/customer_<id>/<sha256>.json
```

Phases: `triage`, `bootstrap`, `tagging`, `claim_tag`, `claim_group`,
`consistency`. The cache key is sha256 over a canonical-JSON payload
of `{model, prompt, system, …extra}`. A model change, prompt edit, or
catalog snapshot change invalidates the cached response automatically.

`LoggingJsonLlm` wraps `CachedJsonLlm` so cache HITs are still logged
at DEBUG — useful when reproducing a past run.

---

## File map

| File | Role |
|---|---|
| `models.py` | All dataclasses (`Customer`, `SourceItem`, `ArticleBundle`, `StanceEntry`, `StanceAssignment`, `ClaimAssignment`, `TagsMessage`, `StreamRunStats`, …) and enums. |
| `catalogs.py` | `StanceCatalog`, `ClaimCatalog`, `ClaimCatalogStore`. The repository surface — the only thing that mutates catalog state. |
| `retrieval.py` | `ArticleBundleRetriever` — reads the pre-linked fixture and yields `ArticleBundle`s. |
| `triage.py` | `TypeTriageStep` — classifies items into typed rows. |
| `tagging.py` | `StanceTagger`, `StanceUpdater`, `ClaimTagger`, `ClaimUpdater` — the streaming-time per-type/per-event LLM steps. |
| `bootstrap.py` | `BootstrapStep` — Phase 1; also called by the consistency pass's Stage 2. |
| `streaming.py` | `StreamingTagsPipeline` + `StreamingState` — wires the four streaming steps and counter bookkeeping. |
| `consistency.py` | `ConsistencyPassStep` — three-stage curation pass. |
| `prompts.py` | Block builders + per-pipeline prompt builders (one per LLM call). Mutates id maps in place. |
| `prompts/*.txt` | Prompt templates (`triage`, `bootstrap_per_type`, `tag_per_type`, `claim_extract`, `claim_group`, `hygiene_per_type`) and 9 per-type guides under `prompts/types/`. |
| `prompts/style_guide.md` | The full prompt-writing style guide. |
| `llm.py` | `JsonLlm` protocol, `OpenRouterJsonLlm`, `CachedJsonLlm`, `LoggingJsonLlm`, prompt-load helpers. |
| `persistence.py` | JSON snapshot read/write for both catalogs. |
| `db.py` | userdb-backed repo classes (`StanceCatalogRepo`, `ClaimCatalogStoreRepo` / `ClaimCatalogRepo`, `EntityStateRepo`) + `connect_userdb()`. Same method surface as `catalogs.py`, plus `set_bundle_context(bundle, query_id)` for per-message enrichment of `parent_source_id` / `news_type` / `query_id`. |
| `source_items.py` | `LocalFileSourceItemFetcher` (local fixture) and `ESSourceItemFetcher` (ES `news` index) — both implement `fetch_for_assignments(assignments)` to rebuild `items_seen` for the consistency pass after a worker restart. |
| `stream.py` | Paste-and-step IPython script — top-level setup + main loop, no function wrapping. Edit `ORG_ID` / `BUNDLE_LIMIT` / paths at the top and run; use `next(messages)` for single-stepping. Reset-state snippet is in the header comment. Imports the per-message machinery from `loop_helpers.py`. |
| `loop_helpers.py` | Production-shaped message-loop helpers: `build_repos`, `handle_message` (per-message TX), `consistency_pass_due`, `run_consistency_pass` (retention + items-seen rebuild + Stage 1/2/3 + commit), `simulated_message_stream` (local-fixture ingestion — swap for a `pika` consumer in production). |
| `runner.py` | `LocalRunConfig` + builder helpers for the CLI entrypoints. |
| `run_tags.py` | IPython driver — Phase 1 → Phase 2 → Phase 3 in one script. |
| `cli/*.py` | Standalone CLI entrypoints for bootstrap / run / consistency. |
| `stats.py`, `test_helpers.py` | Printout helpers for the IPython driver (`run_tags.py`). `test_helpers.py` was the file previously named `loop_helpers.py`. |
| `data_model.md`, `tags_design.md`, `tags_impl_plan.md` | Older design notes; this readme supersedes most of their narrative content. |

---

## Open work

1. **Stance splitting.** Stage 4 of the consistency pass: select
   entries with unusually high `n` (an over-broad stance has absorbed
   too many distinct ideas), send each to a per-stance splitting LLM
   call, re-tag every assigned item into the new sub-stances. The
   open question is the orphan window between split and re-tag — the
   reason this hasn't shipped yet.

2. **Comments in the consistency window.** Currently
   `recent_bundle_assignments` filters to `kinds=("article",
   "user_post")` — comments are excluded from the window. Folding
   them in is a one-line change but requires deciding how to weight
   them (a viral post can have hundreds of comments and dominate the
   sample).

3. **Real RabbitMQ consumer.** The userdb repos
   ([`db.py`](./db.py)), the source-item fetcher
   ([`source_items.py`](./source_items.py)), the per-message helpers
   ([`loop_helpers.py`](./loop_helpers.py)) and the file-simulated
   driver script ([`stream.py`](./stream.py)) are in place. The
   pipeline runs end-to-end against userdb, including retention and
   the ES-backed `items_seen` rebuild for the consistency pass. Still
   to do: replace `simulated_message_stream` in `loop_helpers.py`
   with a `pika` consumer that yields the same `TagsMessage` shape
   and acks after each per-bundle commit.
