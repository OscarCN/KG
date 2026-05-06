# Tags — Implementation Plan (Stage 1, in-memory)

> **Status:** Stage 1 — in-memory only, no DB writes. Source of truth for the design (the *what*) lives in [`tags_overview.md`](tags_overview.md). This document is the *how*: classes, signatures, lifecycle, caching, integration points.

## Scope and non-goals

In scope (Stage 1):
- Customer-anchored stance catalog with bootstrap → tag → adjudicate → apply lifecycle.
- Per-`(customer, event)` claim catalog made of clusters, with cluster freshness flags (`is_new`).
- Streaming integration with `src/entities/linking/run_linking.py` so per-article linker output flows directly into per-event tagging.

Out of scope (deferred to Stage 2):
- Postgres persistence (the snapshot dump is a debug artefact, not a write).
- Live DB reads of the customer record (Stage 1 reads a JSON fixture).
- Posts/social retrieval (only the `news` index is wired).
- Fact verification (we flag novelty, we don't verify).

## Module layout

```
src/entities/tags/
  tags_overview.md           # design spec
  tags_impl_plan.md          # this file
  readme_tags.md             # user-facing how-to
  __init__.py                # re-exports (incl. everything in models/)
  models/                    # pure data structures — no LLMs, no IO
    __init__.py              # re-exports
    customer.py              # Customer + EntityType + EntityLocation + ContentGraphConfig + load_customer_from_json
    source_item.py           # SourceItem (article / user_post / user_comment)
    stance_catalog.py        # StanceCatalog + StanceEntry + StanceAssignment
    claim_catalog.py         # ClaimCatalog + ClaimCluster + RawClaim + ClaimAssignment + ClaimCatalogRegistry
  retrieval.py               # Retrieval (ES wrapper) + LocalFileRetrieval (testing)
  bootstrap.py               # Phase 1 — bootstrap stance catalog
  tagging.py                 # Phase 2 — TaggingOrchestrator + StanceProposal + TaggingResult
  stance_adjudicator.py      # Phase 3 — StanceAdjudicator + AdjudicationDecision
  claim_clusterer.py         # Phase 4 — ClaimClusterer + ClusteringResult
  apply.py                   # Phase 5 — apply tagging + adjudications + clustering into catalogs
  persistence.py             # Persistence Protocol + InMemoryPersistence
  stats.py                   # StreamingStats + snapshot printers
  _llm_io.py                 # shared LLM call/cache/retry helpers
  prompts/
    bootstrap.txt            # Phase 1 prompt
    tagging.txt              # Phase 2 prompt
    adjudicator.txt          # Phase 3 prompt
    clusterer.txt            # Phase 4 prompt
```

The `models/` split is deliberate: every class under it is a pure data structure (no LLM calls, no filesystem, no ES). The Stage-2 Postgres swap replaces only `persistence.py` and `customer.py:load_customer_from_db` — `models/` stays unchanged.

## Class catalog

### `models/customer.py`

The dataclass shape mirrors the kgdb `entities` row + joined helper tables, so the Stage-1 fixture is structurally identical to a Stage-2 DB read.

- **`EntityType`** — `(entity_type_id, entity_type, entity_kind)`. From `entity_types_kinds_available`.
- **`EntityLocation`** — mirrors every column on `kgdb.entity_locations` (all optional except `entity_id`).
- **`Customer`**:
  - kgdb fields: `entity_id: int`, `name`, `description`, `metadata`, `keywords`, `filter_llm_prompt`, `added`, `modified`.
  - joined: `types: list[EntityType]`, `locations: list[EntityLocation]`, `aliases: list[str]`, `related_entity_ids: list[int]`.
  - `slug` property: `f"customer_{entity_id}"` — used for cache dirs and snapshot paths so storage doesn't depend on the (mutable) name.
  - `Customer.from_kgdb_row(row, types, locations, aliases, related_entity_ids)` — used by both the Stage-1 fixture builder and the Stage-2 DB loader.
  - `Customer.from_dict(d)` — reads the fixture JSON.
- **`ContentGraphConfig`** — `customer`, `event_supertypes`, `theme_supertypes`, `notes`. Wraps `Customer` + run-level filtering knobs.
- `load_customer_from_json(path) → ContentGraphConfig` — Stage-1 entry point.
- `load_customer_from_db(entity_id, conn) → ContentGraphConfig` — Stage-2 stub. Raises `NotImplementedError` until DB writes are wired.

### `models/source_item.py`

- **`SourceItem`** — uniform record for articles, posts, and comments. `kind ∈ {"article","user_post","user_comment"}`. Comments carry `parent_source_id` (article URL).

### `retrieval.py`

- **`Retrieval(search_client=None, news_index="news", cache_dir=...)`**:
  - `get_article(source_id)` — single article by `url == source_id`.
  - `get_article_with_comments(source_id) → (article, comments)` — parses the embedded `comments` array on the news doc.
  - `get_event_items(event_id, source_ids)` — articles + comments mixed across the event's source ids (deduped).
  - `get_post_comments(post_id)` — `NotImplementedError` (posts/social index not wired in Stage 1).
  - `get_customer_corpus(source_ids, limit=None)` — bootstrap-time corpus loader.
- Disk cache: `cache/es_articles/<sha256(source_id)>.json` (one entry per article doc).
- **`LocalFileRetrieval(news_json_path)`** — testing helper that loads articles from a local JSON file (e.g. `data/ayuntamiento_qro/*.json`) instead of hitting ES.

### `models/stance_catalog.py`

- **`StanceEntry`** — `id` (slug), `label`, `description`, `created_at`, `n_assignments`, `aliases`.
- **`StanceAssignment`** — `(source_item_id, source_kind, customer_id, stance_id, event_id?, theme_id?, assigned_at, reason)`.
- **`StanceCatalog`**:
  - `add(entry)` / `rename(stance_id, new_label, new_description)` (id-stable; old label appended to `aliases`) / `merge(src_id, dst_id)` (re-points all assignments).
  - `assign(assignment)` — increments `n_assignments`.
  - `reroute_assignments(from_id, to_id)` — used by adjudicator's `generalise` decision to re-point a placeholder's assignments at an existing entry.
  - `drop_assignments_for(stance_id)` — used by adjudicator's `reject` decision on a placeholder.
  - `summary()` — sorted `[(label, count), …]`.
  - `to_dict()` / `from_dict()` for snapshotting.

### `models/claim_catalog.py`

- **`RawClaim`** — `(event_id, customer_id, affected_entity_ids, verbatim, source_id, source_kind, importance ∈ 1..3, importance_reason, extracted_at)`.
- **`ClaimCluster`** (= claim catalog entry): `id`, `event_id`, `customer_id`, `canonical`, `members: list[RawClaim]`, `created_at`, `is_new`, `freshness_window_hours=24`, `aliases`. Properties: `importance_max`, `importance_typical` (median), `importance_n_high` (count of 3-scored members). Methods: `add_member(claim)`, `rename(new_canonical)`, `freshness_expired(now)`.
- **`ClaimAssignment`** — `(source_item_id, source_kind, cluster_id, event_id, customer_id, verbatim, assigned_at)`.
- **`ClaimCatalog`** (per `(customer, event)`):
  - `create_new(claim, canonical)` — new cluster, marked `is_new=true`.
  - `assign_to_existing(claim, cluster_id)`.
  - `rename(cluster_id, new_canonical)` (id-stable).
  - `merge(src_id, dst_id)` (re-points members + assignments).
  - `expire_freshness(now=None)` — flips `is_new=false` on clusters past their freshness window.
- **`ClaimCatalogRegistry`** — holds one `ClaimCatalog` per `(customer_id, event_id)`; `get_or_create((customer_id, event_id))`.

### `bootstrap.py` — Phase 1

- `bootstrap_stance_catalog(customer, corpus, model=OPENROUTER_BOOTSTRAP_MODEL) → StanceCatalog`.
- One LLM call. Prompt loaded from `prompts/bootstrap.txt`. The corpus is mixed source items (articles + posts + comments) the linker has already filtered for relevance.
- Output: a `StanceCatalog` with `StanceEntry`s. No assignments yet.

### `tagging.py` — Phase 2

- **`StanceProposal`** — `kind ∈ {"add","rename"}`, `label`, `description`, `src_stance_id?`, `proposal_id` (used by `apply.py` to thread placeholder entries).
- **`TaggingResult`** — `stance_assignments`, `stance_proposals`, `claims`, `raw_claims_dropped_off_customer`.
- **`TaggingOrchestrator`**:
  - `tag_batch(event_id, event_summary, items) → TaggingResult` — one LLM call per `(event, batch)`.
  - The orchestrator does **NOT** mutate catalogs. Apply is in `apply.py` so adjudication / clustering can run between extract and apply.
  - Phase-2 retention rule: drops every claim where `customer.entity_id ∉ affected_entity_ids` and counts these as `raw_claims_dropped_off_customer`.
  - Stance assignments referencing a `stance_id` not in the current catalog are dropped (proposed labels live in `stance_proposals`, not in assignments).

### `stance_adjudicator.py` — Phase 3

- **`AdjudicationDecision`** — `proposal_index`, `decision ∈ {"accept","reject","rename","generalise"}`, optional `existing_id`, `new_label`, `new_description`, `reason`.
- **`StanceAdjudicator.adjudicate(proposals, sample_items) → list[AdjudicationDecision]`** — one LLM call. Validates `existing_id` against the current catalog; treats invalid ids as `reject`.

### `claim_clusterer.py` — Phase 4

- **`ClusteringDecision`** — `claim_index`, `decision ∈ {"assign","create","drop"}`, optional `cluster_id`, `canonical`, `reason`.
- **`ClusteringMutation`** — `kind ∈ {"rename","merge"}` plus its operands.
- **`ClusteringResult`** — `decisions`, `mutations`.
- **`ClaimClusterer.cluster(raw_claims) → ClusteringResult`** — one LLM call per `(customer, event)` batch. Lighter gate than the adjudicator: just routes raw claims into clusters or drops them.

### `apply.py` — Phase 5

- `stage_proposals_as_placeholders(catalog, proposals) → {proposal_id → stance_id}` — for each `add` proposal, adds a placeholder `StanceEntry` to the catalog. For `rename` proposals, returns the mapping pointing at `src_stance_id` (no placeholder).
- `apply_stance_phase(catalog, customer_id, event_id, tagging, adjudications) → summary_dict` — applies Phase 2 stance assignments, then Phase 3 decisions:
  - `accept` → keep placeholder as-is.
  - `reject` → drop placeholder + its assignments.
  - `rename` → call `catalog.rename(existing_id, new_label, new_description)`; re-point placeholder's assignments at `existing_id`; remove placeholder.
  - `generalise` → re-point placeholder's assignments at `existing_id`; remove placeholder.
- `apply_claim_phase(registry, customer_id, event_id, raw_claims, clustering) → summary_dict` — applies clusterer decisions and mutations.

### `persistence.py`

- **`Persistence`** — `Protocol` with `save_snapshot` and `load_stance_catalog`.
- **`InMemoryPersistence`** — Stage-1 implementation. Snapshot dumps `{stance_catalog, claim_catalogs}` to JSON for inspection.

### `stats.py`

- **`StreamingStats`** — counters absorbed from `apply.py` summaries.
- `print_article_snapshot(...)` — emits the per-article line + top-N stances + new-cluster count.
- `print_event_created_snapshot(...)` — extra block printed whenever the linker creates a new event.

### `_llm_io.py` (shared)

- `render_prompt(template, **fields)` — `{name}` substitution via `str.replace` (no `.format()` so JSON examples in the prompts can use literal `{` and `}`).
- `load_prompt(name)` — read `prompts/<name>.txt`.
- `payload_key(payload)` / `cache_read` / `cache_write` — sha256-keyed disk cache mirroring `link_llm`.
- `parse_json_response(raw)` — defensive JSON parse with markdown-fence stripping.
- `call_with_retry(messages, model, ...)` — 3-attempt retry around `call_openrouter`.
- `call_cached(phase, customer_id, payload, messages, model, use_cache)` — top-level helper; hash-keyed cache lookup, then LLM call, then cache write.
- `customer_context_block(customer)` — uniform JSON serialisation of the customer for inclusion in prompts.

## Pipeline — phase by phase

### Phase 1 — bootstrap (once per customer)

```
bootstrap_stance_catalog(customer, corpus, model=OPENROUTER_BOOTSTRAP_MODEL) → StanceCatalog
```

LLM input: `{customer_context, filter_context, corpus_block}`. Output JSON: `{stances: [{label, description}, …]}`. Cache: `cache/tags_bootstrap/customer_<id>/<sha256>.json`.

### Phase 2 — tag a batch

```
TaggingOrchestrator(customer, stance_catalog, model=OPENROUTER_TAGGER_MODEL)
  .tag_batch(event_id, event_summary, items) → TaggingResult
```

LLM input: `{customer_context, filter_context, event_context, stance_catalog, items_block}`.
Output JSON: `{stance_assignments, stance_proposals, claims}`.
Cache: `cache/tags_tagging/customer_<id>/<sha256>.json`. Hash payload includes the catalog snapshot (so cache invalidates on every catalog mutation).

### Phase 3 — adjudicate proposed catalog mutations

```
StanceAdjudicator(customer, stance_catalog, model=OPENROUTER_ADJUDICATOR_MODEL)
  .adjudicate(proposals, sample_items) → list[AdjudicationDecision]
```

LLM input: `{customer_context, stance_catalog, proposals_block, sample_items_block}`.
Output JSON: `{decisions: [{proposal_index, decision, existing_id?, new_label?, new_description?, reason?}, …]}`.
Cache: `cache/tags_adjudicator/customer_<id>/<sha256>.json`.

### Phase 4 — cluster raw claims into the per-event catalog

```
ClaimClusterer(customer, claim_catalog, event_summary, model=OPENROUTER_CLUSTERER_MODEL)
  .cluster(raw_claims) → ClusteringResult
```

LLM input: `{customer_context, event_context, cluster_catalog, claims_block}`.
Output JSON: `{decisions, mutations}`.
Cache: `cache/tags_clusterer/customer_<id>/<sha256>.json`.

### Phase 5 — apply

Driven by the streaming runner. See `apply.py` above.

## Stance lifecycle (detailed)

1. **Bootstrap.** `bootstrap_stance_catalog(customer, corpus)` produces an initial catalog of customer-anchored qualities (e.g. *"el ayuntamiento es ineficiente"*).
2. **Per-batch tagging.** `TaggingOrchestrator.tag_batch(event_id, event_summary, items)` returns:
   - `stance_assignments` — pointed at existing catalog ids only.
   - `stance_proposals` — `add` (new label) or `rename` (refine an existing entry).
3. **Adjudication.** `StanceAdjudicator.adjudicate(proposals, sample_items)` decides per proposal: `accept` / `reject` / `rename` / `generalise`.
4. **Apply.** `apply_stance_phase` first stages each `add` proposal as a placeholder so subsequent runs can route incoming assignments under it; then applies adjudicator decisions:
   - `accept` → placeholder stays.
   - `reject` → placeholder + its assignments dropped.
   - `rename` → `catalog.rename(existing_id, new_label, new_description)` (id-stable; old label → `aliases`); placeholder's assignments rerouted to `existing_id`.
   - `generalise` → placeholder's assignments rerouted to `existing_id`; placeholder removed.

**Propagation contract.** Renames apply retroactively to *every* assignment carrying the renamed `stance_id`, across every event/theme. Because assignments reference entries by id (not label), this is automatic — no rewrite pass needed.

## Claim lifecycle (detailed)

1. **Per-batch extraction.** Same Phase-2 LLM call. The orchestrator drops claims where `customer.entity_id ∉ affected_entity_ids` (Phase-2 drop, counted in stats).
2. **Clustering.** Per `(customer, event)`, `ClaimClusterer.cluster(raw_claims)` returns `assign` / `create` / `drop` per claim, plus `rename` / `merge` mutations on the catalog.
3. **Apply.**
   - `assign` → `ClaimCatalog.assign_to_existing(claim, cluster_id)`.
   - `create` → `ClaimCatalog.create_new(claim, canonical)` — cluster marked `is_new=true` for `freshness_window_hours` (default 24h).
   - `drop` → discarded but logged (Phase-4 drop, counted separately in stats).
   - `rename` → `cluster.rename(new_canonical)` (id-stable; old canonical → `aliases`).
   - `merge(src, dst)` → members + assignments re-pointed to `dst`; `dst.aliases` keeps old `src` canonical.
4. **`is_new` lifecycle.** Set by `create_new`. The streaming runner calls `ClaimCatalog.expire_freshness(now)` before each snapshot to flip `is_new=false` on clusters past their window. The alert surface is `is_new=true ∧ importance_max ≥ 2`.
5. **Drop policy.** Two distinct drops, counted separately:
   - **Phase 2 drop** — claim filtered because `customer ∉ affected_entity_ids` (retention rule).
   - **Phase 4 drop** — clusterer says `drop` because the claim is too vague / off-customer / duplicative noise.

## LLM model selection

| Phase | Env var | Default model |
|---|---|---|
| Bootstrap | `OPENROUTER_BOOTSTRAP_MODEL` | `openai/gpt-4o` |
| Tagging | `OPENROUTER_TAGGER_MODEL` | `openai/gpt-4o` |
| Adjudicator | `OPENROUTER_ADJUDICATOR_MODEL` | `openai/gpt-4o` |
| Clusterer | `OPENROUTER_CLUSTERER_MODEL` | `google/gemini-2.5-flash-lite` |

All four use `response_format={"type":"json_object"}`, `temperature=0.0`, and the existing `call_openrouter` wrapper. The clusterer reuses the linker's flash-lite default — same gate semantics (route, don't judge), same model.

## Caching contract

One cache directory per phase, scoped by customer:

```
cache/
  tags_bootstrap/customer_<id>/<sha256>.json
  tags_tagging/customer_<id>/<sha256>.json
  tags_adjudicator/customer_<id>/<sha256>.json
  tags_clusterer/customer_<id>/<sha256>.json
  es_articles/<sha256(source_id)>.json
```

Hash key: sha256 over a stable canonical-JSON payload that includes `model`, `phase`, `customer_id`, the relevant catalog snapshot, and the inputs. Same pattern as `src/entities/linking/link_llm.py:_payload_key`.

To invalidate a phase: delete its cache directory.

## Streaming integration

`src/entities/linking/run_linking.py`:

1. Load + group records by `_source_id`, sort by `date_created` ascending.
2. **Per article:**
   1. Fetch `(article, comments)` via `Retrieval.get_article_with_comments(source_id)` (or `LocalFileRetrieval` in test mode).
   2. For each extracted record in the article: call `EntityLinker.link_one(raw)` → `LinkResult(status, event_id, record, reason)`.
   3. For each `LinkResult` with status ∈ {`created`, `merged`}: run Phase 2 (tag), Phase 3 (adjudicate proposals if any), Phase 4 (cluster claims if any), then Phase 5 (apply).
   4. On `status="created"`: emit a per-event snapshot (`print_event_created_snapshot`).
   5. After the article finishes: emit a per-article snapshot (`print_article_snapshot`).
3. After the run: write `data/linked/<input>.json` (legacy) + `data/tags/<customer_slug>/run_<ts>.json` (in-memory snapshot) and print the `StreamingStats` summary.

`LinkResult` (defined in `src/entities/linking/link.py`):

```python
@dataclass
class LinkResult:
    status: Literal["created","merged","skipped","dropped","error"]
    event_id: Optional[str] = None
    record: Optional[dict] = None
    reason: Optional[str] = None
```

`EntityLinker.link_one(raw)` is the streaming entry point; `link_all(records)` becomes a thin loop over `link_one`. The two paths produce the same in-memory state (`linker.events` + `linker.dropped`).

## Stage-2 hooks (forward-looking only)

Each in-memory class has a target Postgres table. **No Postgres in this plan** — recorded here so the Stage-2 swap is mechanical.

| In-memory | Stage-2 target (per `media-backend-paid/docs/DATABASE_POSTGRES.md`) |
|---|---|
| `Customer` | `kgdb.entities` row + `entity_types`, `entity_locations`, `entities_alias`, `relations` joins |
| `StanceCatalog` (entries) | `kgdb.entities.metadata` field on the customer row, OR a dedicated `stance_catalog` table |
| `StanceAssignment` | `userdb.entities_documents_sentiments_org` row anchored on `entity_id = customer.entity_id` (modify or replicate; Q12 in `tags_overview.md`) |
| `ClaimCluster` | `claim_clusters` table (TBD), keyed `(customer_id, event_id, cluster_id)` |
| `ClaimAssignment` | `claim_assignments` table (TBD), m-to-m between docs and clusters |
| Persistence interface | `Persistence` Protocol — drop in a Postgres impl alongside `InMemoryPersistence` |

The `load_customer_from_db(entity_id, conn)` stub in `customer.py` documents where the runtime swap happens (replaces `load_customer_from_json` in `run_linking.py`).

## Open questions

Pinned by this plan:

- **OQ3, OQ9, OQ10** from `tags_overview.md` — content graph upstream / source_kind on assignments / novelty over verification — implemented as designed.

Still open:

- **OQ7** Multi-stance per source item — Phase 2 emits at most one stance per item; the data model already supports a list and adjudication is id-stable, so the upgrade is a prompt change.
- **OQ12** Modify vs replicate `entities_documents_sentiments_org` — Stage 2 decision.
- **OQ13** Where catalogs live in the DB — Stage 2 decision.
- **OQ14** Adjudicator scope (whole catalog vs neighbours) — currently the whole catalog goes in; revisit if cost spikes.
- **OQ15** Cross-customer catalog reuse — out of scope for v1.
