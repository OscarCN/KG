# Tags — Data Model

Dataclass shapes for the `tags` subsystem. Companion to
[`tags_design.md`](tags_design.md).

## Enums

```python
StanceType = Literal[
    "entity_stance",
    "complaint",
    "gratefulness",
    "suggestion",
    "request",
    "denuncia",
    "question",
    "endorsement",
    "noise",
]

# Types that have their own entry-set in the catalog and can grow at
# streaming time. Consolidation operates on the same set.
STANCE_BEARING_TYPES: set[StanceType] = {
    "entity_stance",
    "complaint",
    "gratefulness",
    "suggestion",
    "request",
    "denuncia",
    "question",
    "endorsement",
}

# Types that exist as assignment-only tags (no catalog entry; stance_id=None).
TAG_ONLY_TYPES: set[StanceType] = {"noise"}

SourceKind = Literal["article", "user_post", "user_comment"]
```

## Source data

### `SourceItem`

```text
id: str
kind: SourceKind
text: str
author: str | None
created_at: str | None
parent_source_id: str | None
metadata: dict
```

### `LinkedEventContext`

A compact event view used for prompt context and claim scoping.

```text
id: str
description: str
```

Resolved from `event_ids` against a sibling event store.

### `ArticleBundle`

The streaming unit (see §2 of `tags_design.md`).

```text
root: SourceItem            # kind ∈ {article, user_post}
comments: list[SourceItem]  # kind == user_comment
event_ids: list[str]
linked_events: list[LinkedEventContext]
customer: Customer
```

## Customer

Mirrors `kgdb.entities` plus joined helpers, with consistency-pass state
fields added. `to_dict` / `from_dict` are hand-written.

```text
entity_id: int
name: str
description: str
metadata: dict | None
keywords: list[str] | None
filter_llm_prompt: str | None
added: str
modified: str
types: list[EntityType]
locations: list[EntityLocation]
aliases: list[str]
related_entity_ids: list[int]

# Consistency-pass state
items_processed_total: int = 0
items_processed_since_last_pass: int = 0
last_consistency_pass_at: str | None = None
last_consistency_pass_count: int = 0
consistency_pass_threshold_items: int = 200
consistency_pass_threshold_days: int = 7
```

`Customer.consistency_pass_due(now: datetime) -> bool` returns True when the
item counter has tripped, or when the day-floor has elapsed since the last
pass.

## Stance

### `StanceEntry`

```text
id: str
label: str
description: str
primary_type: StanceType
created_at: str
aliases: list[str]
origin_event_id: str | None
```

`primary_type` is required; assignments must match it.

### `StanceAssignment`

```text
source_item_id: str
source_kind: SourceKind
customer_id: int
stance_id: str | None              # null for noise, or for catalog-bearing types when no entry fits
stance_type: StanceType
event_id: str | None               # filter dimension; not the entry's scope
reason: str
assigned_at: str
```

No `sentiment` field. Polarity for `endorsement` lives in the catalog label
(`apoyo a X` vs `rechazo a X`).

### `StanceProposal`

Emitted by streaming or consolidation; adjudicated by `StanceUpdater`.

```text
kind: Literal["add", "rename"]
label: str
description: str
stance_type: StanceType
source_item_ids: list[str]         # for kind == "add"
src_stance_id: str | None          # for kind == "rename"
```

### `StanceCatalog`

In-memory store. Keeps a single flat `entries: dict[str, StanceEntry]`;
type-scoped queries filter on `primary_type`.

Methods:

- `add_entry(entry)`
- `assign(assignment)` — drops on type mismatch
- `rename(stance_id, new_label, new_description)`
- `merge(src_id, dst_id)`
- `retire(stance_id)` — soft-delete; assignments stay tagged with the old id
- `reroute(from_id, to_id)` — bulk-rewrite assignments without deleting source
- `iter_entries(types=None)` / `summary(types=None)` / `snapshot(types=None)`

### `StanceTagging`

Per-call result of `StanceTagger.tag(...)`.

```text
assignments: list[StanceAssignment]
proposals: list[StanceProposal]
dropped_assignments: int
n_assignments_by_type: dict[StanceType, int]
n_items_tagged_no_stance: int
```

### `StanceDecision`

`StanceUpdater` output, one per `StanceProposal`.

```text
proposal_index: int
action: Literal["accept", "reject", "rename", "generalise"]
existing_id: str | None
new_label: str | None
new_description: str | None
reason: str
```

## Claims

### `RawClaim`

```text
event_id: str
customer_id: int
verbatim: str
source_item_id: str
source_kind: SourceKind
importance: int            # 1 | 2 | 3
importance_reason: str
extracted_at: str
```

### `ClaimCluster`

```text
id: str
customer_id: int
event_id: str
canonical: str
members: list[RawClaim]
aliases: list[str]
created_at: str
importance_max: int
importance_typical: int
importance_n_high: int
is_new: bool
freshness_window_hours: int = 24
```

### `ClaimAssignment`

```text
source_item_id: str
source_kind: SourceKind
cluster_id: str
event_id: str
customer_id: int
verbatim: str
assigned_at: str
```

### `ClaimCatalog` / `ClaimCatalogStore`

`ClaimCatalog` is per `(customer_id, event_id)`. `ClaimCatalogStore` is a
keyed registry keyed on that tuple.

`ClaimCatalog` methods: `assign(claim, cluster_id)` / `create(claim, canonical)` /
`rename(cluster_id, new_canonical)` / `merge(src_id, dst_id)` / `summary()`.

### `ClaimTagging`

```text
claims: list[RawClaim]
dropped_invalid: int
dropped_off_customer: int
```

### `ClaimDecision`

`ClaimUpdater` output, one per `RawClaim`.

```text
claim_index: int
action: Literal["assign", "create", "drop"]
cluster_id: str | None
canonical: str | None
reason: str
```

### `ClaimMutation`

Optional cluster-catalog mutations from the same step.

```text
kind: Literal["rename", "merge"]
cluster_id: str | None     # for rename
new_canonical: str | None  # for rename
src_id: str | None         # for merge
dst_id: str | None         # for merge
```

## Triage

### `TypeTriageItem`

One row per distinct stance idea. An item with two ideas produces two rows;
tie-break (§3 of `tags_design.md`) has already picked the type for each idea.

```text
source_item_id: str
source_kind: SourceKind
stance_type: StanceType             # exactly one type per row
brief_summary: str
importance_hint: Literal["low", "medium", "high"] | None
```

### `TypeTriageResult`

```text
triaged: list[TypeTriageItem]       # multi-stance via multiple rows for the same source_item_id
n_items_seen: int
```

Note: claim extraction is NOT colocated with triage. Claims are extracted in
`ClaimTagger` over `(root, event_id)` pairs only when `event_ids` is
non-empty.

## Step results

### `StepSummary`

Counters emitted by every step (`name`, `inc(key, n=1)`,
`counters: dict[str, int]`, `notes: list[str]`).

### `EventTagResult`

Per-event tagging round result (used inside `ArticleProcessResult`):

```text
event_id: str
stance_tagging: StanceTagging
stance_update: StepSummary
claim_tagging: ClaimTagging
claim_update: StepSummary
```

### `ArticleProcessResult`

Per-`ArticleBundle` round result:

```text
source_id: str
summaries: list[StepSummary]
event_tag_results: list[EventTagResult]
```

### `ConsistencyPassResult`

```text
customer_id: int
started_at: str
finished_at: str
sample_size: int
sample_strategy: dict[str, Any]
proposals: list[StanceProposal]
merge_pairs: list[tuple[str, str]]      # (src_id, dst_id) — intra-type only
retire_ids: list[str]
reroute_pairs: list[tuple[str, str]]    # (from_id, to_id)
decisions: list[StanceDecision]         # 1:1 with proposals
summary: StepSummary
```

## Snapshot persistence

`save_snapshot` serialises through each dataclass's `to_dict()`:

- `StanceAssignment.to_dict` is `dict(self.__dict__)` — new fields auto-propagate.
- `StanceEntry.to_dict`, `Customer.to_dict` / `from_dict` are hand-written —
  changes here need explicit updates.
- Migration on load: missing fields use dataclass defaults.

The snapshot file shape is two top-level keys:

```json
{
  "stance_catalog": {"entries": [...], "assignments": [...]},
  "claim_catalogs": {"<customer_id>|<event_id>": {"clusters": [...], "assignments": [...]}}
}
```
