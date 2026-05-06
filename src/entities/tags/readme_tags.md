# Tags — How to use

User-facing how-to. For the **design** (what stances and claims are, why customer-anchored, why event-scoped), see [`tags_overview.md`](tags_overview.md). For the **architecture** (classes, methods, lifecycle), see [`tags_impl_plan.md`](tags_impl_plan.md).

## What this directory does

For a single **customer entity** (e.g. *Ayuntamiento de Querétaro*, an insurance company) the tags subsystem extracts two complementary kinds of tags from the same corpus of articles, posts, and comments:

- **Stances** — durable attitudes the public expresses about the customer (*"el ayuntamiento es ineficiente"*).
- **Claims** — specific factual assertions made about events involving the customer, grouped into clusters per event.

Stages 1 (in-memory only — what's implemented today) and Stage 2 (Postgres-backed — designed but not wired) are described in `tags_overview.md`.

## Quick start

```bash
ipython src/entities/linking/run_linking.py
```

What happens, in order:

1. The script loads `data/extracted_raw/ayuntamiento_tst.json` (extracted records).
2. Loads the customer fixture from `data/tags/customer_75.json`.
3. Bootstraps the stance catalog once (Phase 1) using a sample of articles + comments tied to the customer.
4. Streams articles one at a time. For each article:
   - Fetches `(article, comments)` from ES (or a local file in test mode).
   - Runs `EntityLinker.link_one(record)` per extracted record.
   - For each linked event from this article: tags the article + comments via Phase 2 (one LLM call → stances + claims), runs Phase 3 adjudicator on any catalog proposals, runs Phase 4 clusterer on the claims, applies everything.
   - Prints a snapshot line per article + a per-event block whenever a new event is created.
5. Writes:
   - `data/linked/ayuntamiento_tst.json` — the linker's canonical events (legacy output).
   - `data/tags/customer_75/run_<ts>.json` — the in-memory snapshot of the stance catalog + every per-event claim catalog.

## Configuration

### Required environment

- `OPENROUTER_API_KEY` — set in `.env.local` at the project root. All four LLM calls go through `src/llm/openrouter`.
- ES credentials (`ELASTIC_HOST`, `ELASTIC_PORT`, `ELASTIC_AUTH`, `ELASTIC_HTTP_CERT`) — only when `Retrieval` is hitting the live ES `news` index. The Stage-1 fixture run uses `LocalFileRetrieval` against `data/ayuntamiento_qro/*.json` instead, which has no ES dependency.

### LLM model overrides (env vars)

| Env var | Default | Phase |
|---|---|---|
| `OPENROUTER_BOOTSTRAP_MODEL` | `openai/gpt-4o` | Phase 1 — bootstrap stance catalog |
| `OPENROUTER_TAGGER_MODEL` | `openai/gpt-4o` | Phase 2 — tag a batch (stances + claims) |
| `OPENROUTER_ADJUDICATOR_MODEL` | `openai/gpt-4o` | Phase 3 — adjudicate stance catalog mutations |
| `OPENROUTER_CLUSTERER_MODEL` | `google/gemini-2.5-flash-lite` | Phase 4 — cluster raw claims |

### Customer fixture

The tagging pipeline is parameterised on a single customer. Stage 1 reads it from a JSON fixture sourced from real kgdb data. To regenerate after a `kgdb.entities` row changes:

```bash
python scripts/build_customer_fixture.py 75
# or, if the fixture already exists:
python scripts/build_customer_fixture.py 75 --force
```

The script connects to kgdb via `psycopg2` (env vars `KGDB_HOST`, `KGDB_PORT`, `KGDB_USER`, `KGDB_PASSWORD`, `KGDB_NAME`) and writes `data/tags/customer_<entity_id>.json`. The fixture mirrors the kgdb columns + joined helper tables exactly, so the Stage-2 swap (live DB read) is mechanical: replace `load_customer_from_json(path)` with `load_customer_from_db(entity_id, conn)` in `run_linking.py`.

### Runner knobs (top of `run_linking.py`)

```python
TAGS_ENABLED: bool = True
CUSTOMER_FIXTURE: Path = .../data/tags/customer_75.json
NEWS_LOCAL: Optional[Path] = .../data/ayuntamiento_qro/<file>.json   # set None to use ES
SNAPSHOT_TOP_N: int = 10
BOOTSTRAP_CORPUS_LIMIT: int = 80
```

Set `TAGS_ENABLED=False` to run only the linker (legacy behaviour, identical output shape).

## Outputs

### Per-article stdout

```
[N/total] <source_id>
      events: created=<a> merged=<b>
      top stances: label1 (n1); label2 (n2); ...
      new claim clusters this article: <k>
```

When a new event is created mid-article, an extra block is printed:

```
      ↳ EVENT CREATED <event_id>
        top stances: ...
        clusters: [NEW imp=3 n=1] <canonical>
                  [old imp=2 n=2] <canonical>
```

### `data/tags/<customer_slug>/run_<ts>.json`

Two top-level keys:

- `stance_catalog` — `{entries: [{id, label, description, n_assignments, aliases, …}], assignments: [{source_item_id, source_kind, customer_id, stance_id, event_id, reason, …}]}`.
- `claim_catalogs` — keyed by `"<customer_id>|<event_id>"`. Each value carries `{clusters: [{id, canonical, members, importance_max, importance_typical, importance_n_high, is_new, aliases, …}], assignments: [...]}`.

Useful for eyeballing whether the stance catalog reads as customer-anchored (enduring qualities) and whether claim clusters describe specific allegations.

## Pipeline at a glance

```
Phase 1 — Bootstrap          src/entities/tags/bootstrap.py
   ↓ produces StanceCatalog with initial entries
Phase 2 — Tag                 src/entities/tags/tagging.py (TaggingOrchestrator)
   ↓ emits assignments + proposals + claims (no catalog mutation)
Phase 3 — Adjudicate          src/entities/tags/stance_adjudicator.py (StanceAdjudicator)
   ↓ accept / reject / rename / generalise per proposal
Phase 4 — Cluster (per event) src/entities/tags/claim_clusterer.py (ClaimClusterer)
   ↓ assign / create / drop per raw claim, plus rename / merge mutations
Phase 5 — Apply               src/entities/tags/apply.py
   → catalogs mutated; stats absorbed
```

The streaming runner (`src/entities/linking/run_linking.py`) drives this loop per article.

## Costs and caching

Every LLM call is cached on disk under a sha256 of a stable canonical-JSON payload (model, customer id, catalog snapshot, inputs). Re-runs hit the cache and skip billing.

```
cache/
  tags_bootstrap/customer_<id>/<sha256>.json   # rare — once per (customer, corpus)
  tags_tagging/customer_<id>/<sha256>.json     # hot — one per (event, batch, catalog snapshot)
  tags_adjudicator/customer_<id>/<sha256>.json # only when proposals exist
  tags_clusterer/customer_<id>/<sha256>.json   # one per (customer, event) batch with raw claims
  es_articles/<sha256(source_id)>.json         # one per article doc
```

To invalidate a phase, delete its cache directory. To force a clean re-run for one customer, delete `cache/tags_*/customer_<id>/`.

## Limitations of Stage 1

- **No Postgres** — everything in memory; the snapshot dump is a debug artefact, not a write. The Stage-2 target tables are documented in `tags_impl_plan.md` and `media-backend-paid/docs/DATABASE_POSTGRES.md`.
- **One stance per source item** — the Phase-2 prompt assigns at most one stance per item. Multi-stance is a forward-compatible upgrade (data model already supports lists).
- **Posts / social-media not wired** — `Retrieval.get_post_comments` raises `NotImplementedError`. Only the news index (with embedded comments) is read. Posts will land when a posts/social ES index is wired.
- **No cross-event claim graph** — claims stay event-local; recurrence across events is a future pass via embedding similarity over canonical phrasings.
- **No fact verification** — we flag `is_new` and `importance ≥ 2` as the alert surface; verifying claims as true/false is out of scope.

## Pointers

- Design spec: [`tags_overview.md`](tags_overview.md)
- Architecture / class spec: [`tags_impl_plan.md`](tags_impl_plan.md)
- Linker docs (events as entities, themes, geocoding): [`../linking/readme_linking.md`](../linking/readme_linking.md)
- Entities overview: [`../readme_entities.md`](../readme_entities.md)
- KG database schema (Stage-2 target tables): [`../../../../media-backend-paid/docs/DATABASE_POSTGRES.md`](../../../../media-backend-paid/docs/DATABASE_POSTGRES.md)
- Customer fixture builder: [`../../../scripts/build_customer_fixture.py`](../../../scripts/build_customer_fixture.py)
