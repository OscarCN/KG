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
6. **Counter bookkeeping** — increment `customer.items_processed_*` and
   `customer.bundles_processed_*`. These drive consistency-pass sizing.

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
window = catalog.recent_bundle_assignments(n_bundles=K,
                                           kinds=("article","user_post"))
```

The window is the K most-recent unique post/article source_item_ids
(comments excluded for now) ranked by `max(assigned_at)`. It returns
every assignment (all kinds, all stance_ids including null) for that
source-id set. Stages 2 and 3 each operate on `window_per_type`.

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
| `iter_entries(*, types=None)` | Active entries, optionally filtered by `primary_type` | `SELECT * FROM stance_entries WHERE primary_type IN (:types) AND retired_at IS NULL` |
| `assignments_for(*, types, stance_id, event_id)` | Assignments matching the given filters | `SELECT * FROM stance_assignments WHERE …` |
| `summary(*, types, event_id, top_n)` | `(label, count)` rows by count desc | `SELECT label, count(*) FROM stance_assignments JOIN stance_entries USING (stance_id) GROUP BY …` |
| `snapshot(*, types)` | Compact prompt-ready entry list | Same as `iter_entries` projected to the prompt fields |
| `recent_bundle_assignments(*, n_bundles, kinds)` | All assignments belonging to the K most-recent post/article bundles | See SQL sketch below |

`recent_bundle_assignments` is the windowed query used by the
consistency pass. It groups assignments by `source_item_id` among the
given `kinds`, ranks by `max(assigned_at)` desc, takes the top K, and
returns every assignment (all kinds, all stance_ids) for that set:

```sql
WITH recent AS (
    SELECT source_item_id, MAX(assigned_at) AS last_at
    FROM stance_assignments
    WHERE source_kind IN (:kinds)
    GROUP BY source_item_id
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

Each in-memory shape maps cleanly to a relational table. Rough
column sketch:

```sql
-- StanceEntry
CREATE TABLE stance_entries (
    id              TEXT PRIMARY KEY,        -- "complaint__demora-pago__a1b2c3"
    customer_id     INT  NOT NULL,
    label           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    primary_type    TEXT NOT NULL,           -- StanceType
    created_at      TIMESTAMPTZ NOT NULL,
    retired_at      TIMESTAMPTZ,             -- NULL = active
    aliases         JSONB NOT NULL DEFAULT '[]',
    origin_event_id TEXT
);
CREATE INDEX ON stance_entries (customer_id, primary_type) WHERE retired_at IS NULL;

-- StanceAssignment
CREATE TABLE stance_assignments (
    source_item_id TEXT NOT NULL,
    source_kind    TEXT NOT NULL,            -- 'article' | 'user_post' | 'user_comment'
    customer_id    INT  NOT NULL,
    stance_id      TEXT,                     -- nullable
    stance_type    TEXT NOT NULL,
    event_id       TEXT,
    reason         TEXT NOT NULL DEFAULT '',
    assigned_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source_item_id, customer_id, stance_type, stance_id)
);
CREATE INDEX ON stance_assignments (customer_id, stance_type, assigned_at DESC);
CREATE INDEX ON stance_assignments (stance_id);

-- Customer counters (joined onto kgdb.entities)
ALTER TABLE entities ADD COLUMN bundles_processed_total              INT DEFAULT 0;
ALTER TABLE entities ADD COLUMN bundles_processed_since_last_pass    INT DEFAULT 0;
ALTER TABLE entities ADD COLUMN items_processed_total                INT DEFAULT 0;
ALTER TABLE entities ADD COLUMN items_processed_since_last_pass      INT DEFAULT 0;
ALTER TABLE entities ADD COLUMN last_consistency_pass_at             TIMESTAMPTZ;
```

Equivalent shape for `ClaimCluster` / `ClaimAssignment` keyed by
`(customer_id, event_id)`.

### How the DB switch looks in code

Replace the in-memory `StanceCatalog` with a `StanceCatalogRepo` that
exposes the **same** method names but issues SQL:

- `add(…)` → `INSERT … RETURNING *`
- `assign(a)` → `INSERT INTO stance_assignments …`
- `merge(src, dst)` → transaction: `UPDATE stance_assignments SET stance_id=:dst WHERE stance_id=:src`, then `UPDATE stance_entries SET aliases = aliases || :src_label WHERE id = :dst`, then `DELETE FROM stance_entries WHERE id = :src` (or set `retired_at`).
- `recent_bundle_assignments(…)` → the windowed SQL above.

The streaming pipeline, bootstrap step, and consistency pass don't
need to change — they only ever call these methods.

### Where bundles come from in the DB world

In the local fixture, `ArticleBundleRetriever` reads two JSON files
(linked docs + events store) and yields `ArticleBundle`s. In the DB
world the equivalent is a query that, for one customer:

1. Selects post/article rows from the source store (Elasticsearch or
   a `posts` table) that haven't been tagged yet for this customer,
   ordered by `created_at`.
2. For each root: joins its comments and the linked events from
   `event_links` / `events`.
3. Yields one `ArticleBundle` per root.

The customer-side equivalent of "what's left to process" is:

```sql
-- Bundles still owed to a customer
SELECT p.url AS source_id
FROM posts p
LEFT JOIN stance_assignments sa
  ON sa.source_item_id = p.url AND sa.customer_id = :customer
WHERE p.customer_id = :customer
  AND sa.source_item_id IS NULL
ORDER BY p.created_at;
```

### Selecting past items for consistency passes

The consistency pass already uses `recent_bundle_assignments` — that
query gives it everything it needs. No need to re-fetch source-item
text from a separate store, **as long as** the items themselves are
either still in `state.items_seen` or readable from the source store.
The pass uses `items_seen.get(source_item_id)` to fetch text for the
hygiene samples; the DB equivalent is a join against the posts table.

### Wiring catalogs from assignments

When the system restarts, the catalog is reconstructed from its rows:

```python
# In-memory snapshot (current)
stance_catalog, claim_catalogs = load_snapshot(path)

# DB equivalent (future)
stance_catalog = StanceCatalogRepo(db, customer_id=…)
# entries, retired_entries, assignments are query-backed, not loaded
```

The `to_dict` / `from_dict` round-trip already mirrors the shape that a
SQL backing store would expose.

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
| `models.py` | All dataclasses (`Customer`, `SourceItem`, `ArticleBundle`, `StanceEntry`, `StanceAssignment`, …) and enums. |
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
| `runner.py` | `LocalRunConfig` + builder helpers for the CLI entrypoints. |
| `run_tags.py` | IPython driver — Phase 1 → Phase 2 → Phase 3 in one script. |
| `cli/*.py` | Standalone CLI entrypoints for bootstrap / run / consistency. |
| `stats.py`, `loop_helpers.py` | Printout helpers for the IPython driver. |
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

3. **DB-backed `StanceCatalogRepo`.** Replace the in-memory dicts
   with a SQL implementation behind the same method names. The hot
   query is `recent_bundle_assignments`; everything else is a
   straightforward CRUD mapping.
