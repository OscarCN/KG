# Tags — Implementation Plan

The **how** for the design in [`tags_design.md`](tags_design.md). For
dataclass shapes, see [`data_model.md`](data_model.md). Every section here
references the design doc by `§n`.

## 1. Status & Scope

**Stage 1, in-memory.** Pipelines run end-to-end against local files; no DB,
no live ES. Catalogs are JSON snapshots on disk.

**Deterministic updaters.** `StanceUpdater` and the consistency pass apply
mutations directly through `StanceCatalog` methods after validation — no
adjudicator LLM (§5.4, §5.7).

**ArticleBundle-driven streaming** (§4). Linked event ids are pre-computed
by a sibling pre-link script (§7.2 of the design); the tags pipeline never
calls extraction or linking.

**Out of scope for this slice:** prompt content (rewritten in a follow-up
per H1/H2 of `questions.md`), DB persistence (Stage 2 — design §8), and
deletion of `tags_legacy/` or stale duplicate design files at
`src/entities/tags_gpt/`.

## 2. Module Layout

```
src/entities/tags/
  __init__.py                         # re-exports public API
  tags_design.md                      # design doc (exists)
  data_model.md                       # dataclass shapes (exists)
  questions.md                        # planning artifact (exists)
  tags_impl_plan.md                   # THIS FILE
  readme_tags.md                      # user-facing how-to (post-impl)

  models.py                           # every dataclass in data_model.md
  catalogs.py                         # StanceCatalog, ClaimCatalog, ClaimCatalogStore
  retrieval.py                        # ArticleBundleRetriever
  llm.py                              # JsonLlm protocol + cache + retry
  prompts.py                          # prompt-builder functions

  triage.py                           # TypeTriageStep
  tagging.py                          # StanceTagger, StanceUpdater, ClaimTagger, ClaimUpdater
  bootstrap.py                        # BootstrapStep
  consistency.py                      # ConsistencyPassStep
  streaming.py                        # StreamingTagsPipeline + StreamingState
  persistence.py                      # load_snapshot / save_snapshot
  runner.py                           # LocalRunConfig + run_local_stream
  stats.py                            # incremental snapshot printers

  cli/
    __init__.py
    bootstrap.py                      # `python -m src.entities.tags.cli.bootstrap`
    run.py                            # `python -m src.entities.tags.cli.run`
    consistency.py                    # `python -m src.entities.tags.cli.consistency`

  prompts/                            # text templates (content in H1 follow-up)
    triage.txt
    bootstrap_per_type.txt
    tag_per_type.txt
    claim_extract.txt
    claim_group.txt
    consistency_per_type.txt
    types/
      entity_stance.txt
      complaint.txt
      gratefulness.txt
      suggestion.txt
      request.txt
      denuncia.txt
      question.txt
      endorsement.txt
      noise.txt

scripts/
  build_customer_fixture.py           # exists
  build_linked_fixture.py             # NEW — extraction + linking pre-pass
```

## 3. Class Catalog

### `models.py`

All dataclasses from `data_model.md`. Single file. Includes:

- enums: `StanceType`, `SourceKind`, `STANCE_BEARING_TYPES`,
  `TAG_ONLY_TYPES`.
- source: `SourceItem`, `LinkedEventContext`, `ArticleBundle`.
- customer: `EntityType`, `EntityLocation`, `Customer` (hand-written
  `to_dict` / `from_dict` / `consistency_pass_due`).
- stance: `StanceEntry`, `StanceAssignment`, `StanceProposal`,
  `StanceTagging`, `StanceDecision`.
- claim: `RawClaim`, `ClaimCluster`, `ClaimAssignment`, `ClaimTagging`,
  `ClaimDecision`, `ClaimMutation`.
- triage: `TypeTriageItem` (one row per idea — §5.2),
  `TypeTriageResult`.
- step results: `StepSummary`, `EventTagResult`, `ArticleProcessResult`,
  `ConsistencyPassResult`.

Persistence rule: `StanceAssignment.to_dict = dict(self.__dict__)` (auto-
propagation); `StanceEntry.to_dict` and `Customer.to_dict` are hand-written.

### `catalogs.py`

```python
class StanceCatalog:
    customer_id: int
    entries: dict[str, StanceEntry]            # flat, single-namespace
    retired_entries: dict[str, StanceEntry]
    assignments: list[StanceAssignment]

    def add_entry(self, entry: StanceEntry) -> None
    def assign(self, a: StanceAssignment) -> bool       # rejects on type mismatch
    def rename(self, stance_id: str, new_label: str, new_description: str) -> None
    def merge(self, src_id: str, dst_id: str) -> int
    def retire(self, stance_id: str) -> bool            # soft-delete
    def reroute(self, from_id: str, to_id: str) -> int  # bulk-rewrite, both endpoints stay
    def iter_entries(self, types: set[StanceType] | None = None) -> Iterable[StanceEntry]
    def summary(self, *, types=None, event_id=None, top_n=None) -> list[tuple[str, int]]
    def snapshot(self, *, types=None) -> dict
    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, payload: dict) -> "StanceCatalog"

class ClaimCatalog:
    customer_id: int
    event_id: str
    clusters: dict[str, ClaimCluster]
    assignments: list[ClaimAssignment]

    def assign(self, claim: RawClaim, cluster_id: str) -> ClaimAssignment
    def create(self, claim: RawClaim, canonical: str) -> ClaimCluster
    def rename(self, cluster_id: str, new_canonical: str) -> None
    def merge(self, src_id: str, dst_id: str) -> int
    def summary(self) -> list[tuple[str, int, int, bool]]   # (canonical, n, importance_max, is_new)
    def to_dict(self) -> dict

class ClaimCatalogStore:
    catalogs: dict[tuple[int, str], ClaimCatalog]

    def get_or_create(self, customer_id: int, event_id: str) -> ClaimCatalog
    def iter_for_event(self, event_id: str) -> Iterable[ClaimCatalog]
    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, payload: dict) -> "ClaimCatalogStore"
```

Invariants: `entries` is a single global-id dict; type scoping is a filter
on `primary_type`. `assign()` drops on `assignment.stance_type !=
entry.primary_type`. `rename()` is id-stable. `retire()` keeps existing
assignments tagged with the retired id; new tagging cannot reach it.

### `retrieval.py`

```python
class ArticleBundleRetriever:
    def __init__(self, linked_path: Path, events_path: Path, customer: Customer): ...
    def iter_bundles(self) -> Iterator[ArticleBundle]: ...
    def bundle_for(self, source_id: str) -> ArticleBundle | None: ...
```

Reads the two-file layout from §7.2 of the design:
`<source>.json` (documents enriched with `event_ids`) and
`<source>__events.json` (flat dict keyed by `event_id` →
`LinkedEventContext`).

### `llm.py`

Copy-adapted from `tags_legacy/_llm_io.py`. No imports from `tags_legacy`.

```python
class JsonLlm(Protocol):
    def call(self, prompt: str, *, response_format: dict | None = None) -> dict: ...

class OpenRouterJsonLlm:
    def __init__(self, *, model: str, temperature: float = 0.0, retries: int = 3): ...
    def call(self, prompt: str, *, response_format=None) -> dict: ...

class CachedJsonLlm:
    def __init__(self, inner: JsonLlm, *, cache_dir: Path, payload_key_extra: dict | None = None): ...
    def call(self, prompt: str, *, response_format=None) -> dict: ...

# Helpers
def payload_key(payload: dict) -> str                     # sha256 of canonical JSON
def cache_dir_for(phase: str, customer_id: int) -> Path   # cache/tags_<phase>/customer_<id>/
def cache_read(cache_dir: Path, key: str) -> dict | None
def cache_write(cache_dir: Path, key: str, value: dict) -> None
def parse_json_response(raw: str) -> dict | None
def render_prompt(template: str, **fields) -> str         # str.replace("{name}", value)
def load_prompt(name: str) -> str                         # reads tags/prompts/<name>.txt
```

### `prompts.py`

One builder per pipeline. Each loads its template, injects the per-type
guide where appropriate (from `tags/prompts/types/<type>.txt`), and renders
field substitutions.

```python
def triage_prompt(customer, items, event=None) -> str
def bootstrap_prompt_for_type(customer, stance_type, occurrences) -> str
def tag_prompt_for_type(customer, event, items, triage_hints, catalog_slice, stance_type) -> str
def claim_extract_prompt(customer, event, items, existing_clusters, include_comments) -> str
def claim_group_prompt(customer, event, raw_claims, existing_clusters) -> str
def consistency_prompt_for_type(customer, stance_type, catalog_slice, assignment_sample, item_samples, claim_summaries) -> str
```

Compact-context rules from §6 of the design are enforced inside each
builder (e.g. `customer_block(customer)` strips out aliases / locations /
geocoding when the prompt doesn't need them).

### `triage.py`

```python
class TypeTriageStep:
    def __init__(self, customer: Customer, llm: JsonLlm, *, model: str | None = None): ...
    def triage(self, items: list[SourceItem], event: LinkedEventContext | None = None) -> TypeTriageResult: ...
```

Output validation (§5.2):
- one row per stance idea (`stance_type` is a single `StanceType`),
- multiple rows per `source_item_id` allowed,
- `noise` row collapses siblings for that item,
- soft cap: 4 rows per item,
- unknown local ids are dropped and counted on `n_items_seen`.

### `tagging.py`

```python
class StanceTagger:
    def __init__(self, customer, llm, *, model=None): ...
    def tag(
        self,
        catalog: StanceCatalog,
        *,
        stance_type: StanceType,
        items: list[SourceItem],
        triage_hints: list[TypeTriageItem],
        event: LinkedEventContext | None = None,
    ) -> StanceTagging: ...

class StanceUpdater:
    def __init__(self): ...                      # no LLM
    def update(self, catalog: StanceCatalog, tagging: StanceTagging) -> StepSummary: ...

class ClaimTagger:
    def __init__(self, customer, llm, *, model=None, include_comments: bool = False): ...
    def tag(
        self,
        event: LinkedEventContext,
        root: SourceItem,
        comments: list[SourceItem],
        existing_clusters: list[ClaimCluster],
    ) -> ClaimTagging: ...

class ClaimUpdater:
    def __init__(self, customer, llm, *, model=None): ...
    def update(self, catalog: ClaimCatalog, event: LinkedEventContext, raw_claims: list[RawClaim]) -> StepSummary: ...
```

### `bootstrap.py`

```python
class BootstrapStep:
    def __init__(self, customer, triage_step: TypeTriageStep, llm, *, model=None,
                 min_evidence: int = 2, max_per_type: int = 15): ...
    def run(self, corpus: list[ArticleBundle]) -> StanceCatalog: ...
```

Steps (matches design §5.1):
1. Triage every item across the corpus via `triage_step`.
2. Drop `TAG_ONLY_TYPES` (`noise`).
3. Group `TypeTriageItem`s by `stance_type`.
4. Per `STANCE_BEARING_TYPE`, one `bootstrap_prompt_for_type` LLM call,
   single-shot, full occurrence set passed in.
5. Validate (`label` non-empty, `min_evidence` distinct local evidence ids,
   ≤ `max_per_type`).
6. Add survivors via `catalog.add_entry`.

### `consistency.py`

```python
class ConsistencyPassStep:
    def __init__(self, customer, llm, stance_updater: StanceUpdater, *,
                 model=None, sample_size: int = 300): ...
    def run(
        self,
        catalog: StanceCatalog,
        recent_assignments: list[StanceAssignment],
        items_seen: dict[str, SourceItem],
        claim_catalogs: ClaimCatalogStore,
    ) -> ConsistencyPassResult: ...
```

Steps (matches design §5.7):
1. Stratified sample by `assignment.stance_type`; oversample `stance_id is None`.
2. Group sample by `stance_type` (the type already lives on each row — no re-triage).
3. Per active type, one `consistency_prompt_for_type` LLM call returning
   proposals + `merge_pairs` + `retire_ids` + `reroute_pairs`.
4. Validate, then apply directly through `StanceCatalog.{add_entry,
   rename, merge, retire, reroute}`. No adjudicator LLM.

### `streaming.py`

```python
@dataclass
class StreamingState:
    customer: Customer
    stance_catalog: StanceCatalog
    claim_catalogs: ClaimCatalogStore
    items_seen: dict[str, SourceItem] = field(default_factory=dict)

class StreamingTagsPipeline:
    def __init__(self, *, state: StreamingState, retriever: ArticleBundleRetriever,
                 triage_step: TypeTriageStep, stance_tagger: StanceTagger,
                 stance_updater: StanceUpdater, claim_tagger: ClaimTagger,
                 claim_updater: ClaimUpdater): ...
    def process_bundle(self, bundle: ArticleBundle) -> ArticleProcessResult: ...
```

Per-bundle flow (matches design §4 processing order):
1. Remember `bundle.root` + `bundle.comments` in `state.items_seen`.
2. `triage_step.triage(items=root + comments, event=optional)` → `TypeTriageResult`.
3. Identify active types: `{hint.stance_type for hint in triage.triaged} ∩ STANCE_BEARING_TYPES`.
4. For each active type: `StanceTagger.tag(...)` → `StanceUpdater.update(...)`.
5. Emit `noise` assignments directly from triage rows (no tag call).
6. If `bundle.event_ids`: per `(root, event_id)`, `ClaimTagger.tag(...)` →
   `ClaimUpdater.update(...)` against the per-event `ClaimCatalog`.
7. `customer.items_processed_total += 1`,
   `customer.items_processed_since_last_pass += 1`.
8. Return `ArticleProcessResult` with summaries.

The runner (§9) checks `customer.consistency_pass_due(now)` after each
bundle and dispatches to `ConsistencyPassStep` when due.

### `persistence.py`

```python
def load_snapshot(path: Path) -> tuple[StanceCatalog, ClaimCatalogStore]
def save_snapshot(stance_catalog: StanceCatalog, claim_catalogs: ClaimCatalogStore, path: Path) -> None
```

Two-key JSON shape from `data_model.md`. Round-trip via each dataclass's
`to_dict` / `from_dict`.

### `runner.py`

```python
@dataclass
class LocalRunConfig:
    customer_path: Path
    linked_path: Path
    events_path: Path
    output_dir: Path
    catalog_path: Path | None = None        # bootstrap output for streaming
    include_comments: bool = False
    triage_model: str = ...                 # env-var defaults below
    bootstrap_model: str = ...
    tagger_model: str = ...
    claim_tagger_model: str = ...
    claim_updater_model: str = ...
    consistency_model: str = ...
    sample_size: int = 300
    snapshot_top_n: int = 10

def run_local_stream(config: LocalRunConfig) -> ArticleProcessResult: ...
def run_local_bootstrap(config: LocalRunConfig) -> StanceCatalog: ...
def run_local_consistency(config: LocalRunConfig) -> ConsistencyPassResult: ...
```

### `stats.py`

Copy-adapted from `tags_legacy/stats.py`. Public API:

```python
class StreamingStats: ...
def print_article_snapshot(stats, stance_catalog, claim_catalogs, *, top_n: int) -> None
def print_event_created_snapshot(stance_catalog, claim_catalogs, event_id, *, top_n: int) -> None
def print_sample_source_items(stance_catalog, claim_catalogs, items_seen, *, n: int) -> None
def print_top_events(events, stance_catalog, claim_catalogs, items_seen, customer_id, *, n_events: int, items_per_event: int) -> None
```

### `cli/`

Three thin scripts. Each parses CLI args + env vars into a
`LocalRunConfig` and calls into `runner.py`.

```
python -m src.entities.tags.cli.bootstrap     --customer 75 --corpus data/linked/<file>.json
python -m src.entities.tags.cli.run           --customer 75 --corpus data/linked/<file>.json --catalog data/tags/customer_75/bootstrap.json
python -m src.entities.tags.cli.consistency   --customer 75 --catalog data/tags/customer_75/run_<ts>.json
```

## 4. Pipeline Phase-by-Phase

| § | Phase | Owning class | Prompt template | Cache key inputs | Output dataclass |
|---|---|---|---|---|---|
| 5.2 | Type Triage | `TypeTriageStep.triage` | `triage.txt` | model, customer_id, event_id\|null, items_payload | `TypeTriageResult` |
| 5.3 | Stance Tagging | `StanceTagger.tag` | `tag_per_type.txt` + `types/<type>.txt` | model, customer_id, stance_type, event_id\|null, items_payload, catalog_slice_snapshot | `StanceTagging` |
| 5.4 | Stance Update | `StanceUpdater.update` | (none — no LLM) | n/a | `StepSummary` |
| 5.5 | Claim Extract | `ClaimTagger.tag` | `claim_extract.txt` | model, customer_id, event_id, root_id, items_payload, existing_cluster_canonicals | `ClaimTagging` |
| 5.6 | Claim Group | `ClaimUpdater.update` | `claim_group.txt` | model, customer_id, event_id, raw_claims_payload, cluster_snapshot | `StepSummary` |
| 5.1 | Bootstrap | `BootstrapStep.run` | `bootstrap_per_type.txt` + `types/<type>.txt` | model, customer_id, stance_type, occurrences_payload | `StanceCatalog` |
| 5.7 | Consistency | `ConsistencyPassStep.run` | `consistency_per_type.txt` + `types/<type>.txt` | model, customer_id, stance_type, catalog_slice_snapshot, assignment_sample, item_samples, claim_summaries | `ConsistencyPassResult` |

Cache directory per phase: `cache/tags_<phase>/customer_<id>/<sha256>.json`,
where `<phase>` ∈ `{triage, tagging, claim_tag, claim_group, bootstrap,
consistency}`.

## 5. Stance Lifecycle

1. **Bootstrap** (§5.1) — typed `StanceCatalog` is built once per customer
   from the corpus; `noise` skipped; entries declare `primary_type`.
2. **Streaming tag** (§5.3) — `StanceTagger.tag(stance_type=...)` emits
   `StanceAssignment` rows (with `stance_id` matching an entry of that
   type, or `null`) and `StanceProposal` rows (`add` or `rename`).
3. **Streaming update** (§5.4, deterministic) — `StanceUpdater.update`:
   - apply `assign` if `stance_type == entry.primary_type` (else
     `reject`),
   - apply `add` proposals — create new entry; route originating
     assignments to it,
   - apply `rename` proposals — id-stable rewrite of label / description,
   - drop on validation failure.
4. **Consistency** (§5.7) — bulk add / rename / merge / retire / reroute
   over a stratified sample, applied directly through `StanceCatalog`.

Invariants:

- `assignment.stance_type` always matches the target entry's `primary_type`
  (or `stance_id is None`).
- Renames keep ids stable; existing assignments follow transparently.
- Retired entries stay in `retired_entries` for history; new tagging cannot
  assign to them.
- `merge` can only be intra-type; cross-type collisions are
  `reroute`+`retire` (rare, manual).

## 6. Claim Lifecycle

1. **Skip if no event** (§5.5) — items with empty `event_ids` never call
   the claim extractor.
2. **Extract** (§5.5) — per `(root, event_id)`. Existing cluster
   canonicals attached as de-dup awareness only; no cluster-id assignment
   in this step.
3. **Group** (§5.6) — `ClaimUpdater.update` routes raw claims into
   clusters: assign / create / drop, plus optional rename / merge mutations.
4. **Per-event scope** — each `(customer_id, event_id)` has its own
   `ClaimCatalog` in the `ClaimCatalogStore`.

## 7. LLM Models

Env-var table; defaults pick a quality model on the catalog-shaping calls
and a cheaper one on hot-path classifiers.

| Phase | Env var | Default |
|---|---|---|
| Triage | `OPENROUTER_TAGS_TRIAGE_MODEL` | `google/gemini-2.5-flash-lite` |
| Stance tagging (per type) | `OPENROUTER_TAGS_TAGGER_MODEL` | `openai/gpt-4o` |
| Bootstrap (per type) | `OPENROUTER_TAGS_BOOTSTRAP_MODEL` | `openai/gpt-4o` |
| Claim extract | `OPENROUTER_TAGS_CLAIM_TAGGER_MODEL` | `openai/gpt-4o` |
| Claim group | `OPENROUTER_TAGS_CLAIM_UPDATER_MODEL` | `google/gemini-2.5-flash-lite` |
| Consistency (per type) | `OPENROUTER_TAGS_CONSISTENCY_MODEL` | `openai/gpt-4o` |

All calls use `response_format={"type": "json_object"}`, `temperature=0.2`,
3-attempt retry. `StanceUpdater` runs no LLM.

## 8. Caching Contract

Cache lives under `cache/tags_<phase>/customer_<id>/<sha256>.json`. Key
inputs (canonical JSON, sorted keys) per phase are listed in §4. The cache
is read-through: a hit returns the parsed JSON; a miss calls the model and
writes the response.

Invalidating is a `rm -rf cache/tags_<phase>/customer_<id>/`. Catalog
mutations naturally invalidate downstream caches because the
`catalog_slice_snapshot` field is part of the key.

## 9. Pre-link Script

`scripts/build_linked_fixture.py` produces the inputs the tags pipeline
consumes (§7.2 of the design).

CLI:

```
python scripts/build_linked_fixture.py \
  --raw      data/ayuntamiento_qro/<file>.json \
  --customer data/tags/customer_75.json \
  --out-dir  data/linked/
```

Steps:

1. Load raw documents.
2. For each root, run extraction (`src/entities/extraction/extract.py`,
   `Ontology` pipeline).
3. For each extracted record, call
   `EntityLinker.link_one(raw)` (`src/entities/linking/link.py:245`) →
   `LinkResult`.
4. Aggregate `event_ids` per source document (only `created` / `merged`
   results).
5. Write enriched fixture: `<out-dir>/<source>.json` — original document
   shape + `event_ids: list[str]`.
6. Write event store: `<out-dir>/<source>__events.json` — flat dict keyed
   by `event_id`, value `{id, description, event_type, name}`
   (`LinkedEventContext` shape).

Reuses the customer/linker setup from `src/entities/linking/run_linking.py`.

## 10. CLI Contract

| Command | Reads | Writes |
|---|---|---|
| `cli.bootstrap` | `--corpus <linked>.json`, `--customer <fixture>.json` | `data/tags/customer_<id>/bootstrap.json` |
| `cli.run` | `--corpus <linked>.json`, `--catalog <bootstrap>.json` | per-bundle stdout snapshots; `data/tags/customer_<id>/run_<ts>.json` |
| `cli.consistency` | `--catalog <run>.json` | `data/tags/customer_<id>/consistency_<ts>.json` |

Each command:

- builds a `LocalRunConfig`,
- instantiates `OpenRouterJsonLlm` + `CachedJsonLlm` per phase,
- delegates to `runner.run_local_*`,
- prints a final summary block via `stats.print_*`.

## 11. Stage-2 Hooks

Stage 2 (design §8) replaces the file-based persistence layer with kgdb
loaders/savers. Swap points named:

- `persistence.load_snapshot` / `save_snapshot` → DB-backed equivalents.
- `runner.LocalRunConfig` paths → DB connection params.
- `retrieval.ArticleBundleRetriever` (file-based) → `KgdbBundleRetriever`
  (DB-backed, same `iter_bundles` API).

The dataclass shapes mirror the kgdb tables one-to-one (per the table in
design §8), so the swap is loader/saver only — no consumer-side rewrites
in the streaming pipeline.

## 12. Implementation Order

1. **This file** — `tags_impl_plan.md`.
2. **Data model + catalogs** — `models.py`, `catalogs.py`, `persistence.py`
   with self-roundtrip checks.
3. **LLM scaffolding** — `llm.py`, `prompts.py` (stubs that load text +
   render fields), placeholder `tags/prompts/*.txt`.
4. **Pre-link script** — `scripts/build_linked_fixture.py`. Verify against
   `data/ayuntamiento_qro/ayuntamiento_qro_20260506_175946.json`.
5. **Retrieval** — `retrieval.py`.
6. **Triage + stance streaming** — `triage.py`, `tagging.py` (Stance
   half), `streaming.py` (stance branch only), `cli/run.py`. Eyeball
   stdout snapshots.
7. **Claim half** — `tagging.py` (Claim half), `streaming.py` claim branch.
8. **Bootstrap** — `bootstrap.py`, `cli/bootstrap.py`. Inspect per-type
   entries.
9. **Consistency** — `consistency.py`, `cli/consistency.py`.
10. **Prompts rewrite** — fill `tags/prompts/*.txt` (separate slice per
    H1/H2).
11. **`readme_tags.md`** — user-facing how-to.
12. **Cleanup** — drop the duplicate design files at
    `src/entities/tags_gpt/`. Keep `tags_legacy/` for reference until the
    new impl is fully validated; delete in a later pass.

## 13. Open Questions

1. **Per-type prompts vs parameterized.** The plan picks ONE
   `tag_per_type.txt` + per-type guide (`types/<type>.txt`) injected as
   `{stance_type_guide}`. Alternative: one prompt file per type. Revisit
   in slice 10 (prompt rewrite) once the parameterized version is
   exercised; flip to per-type files only if a type genuinely needs
   different structural guidance.
   ANSWER: Do parameterized, so we can specify/change for every type everywhere
2. **Bootstrap LLM call shape under heavy occurrence load.** §5.1 says
   one-shot per type; if a corpus produces 1000+ occurrences of a single
   type, the prompt may not fit. Cheap mitigation: cap occurrences per
   call to ~150, take a stratified sample. Defer; first hit a real
   corpus and measure.
3. **Consistency-pass cadence in streaming.** `Customer` carries the
   threshold knobs (§7.6 of `data_model.md`); the runner reads them to
   dispatch. Open question: should `cli.run` auto-dispatch the
   consistency pass when due, or only flag it? Recommend auto-dispatch in
   v1 (cheap, deterministic), make it skippable via a flag.
4. **`tags_legacy/` fate.** Keep for now (read-only, no imports from new
   code per §3 `llm.py` spec). Remove in a separate cleanup slice once
   the new impl produces equivalent output on the test fixture.
5. **Sentiment-removal fallout in legacy fixtures.** Snapshots written by
   `tags_legacy/` carry a `sentiment` field on assignments. Loader is
   tolerant — unknown fields ignored. Confirm with a one-time round-trip
   load of `data/tags/customer_75/run_<ts>.json` after the new
   `persistence.py` lands.
