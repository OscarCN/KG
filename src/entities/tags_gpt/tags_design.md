# Tags Design Specification

## 1. Purpose

`tags` is the customer-facing tagging system. Given a stream of article/post
threads (each with comments and pre-linked event ids), it produces:

- typed stance assignments over a per-customer stance catalog,
- per-event factual claim clusters,
- periodic catalog updates and consolidation proposals.

The primary stream unit is an **article or social post with its comments**.
Linked event ids are read as attached metadata; the tagging pipeline does not
invoke the linker. A separate offline pass is responsible for extraction +
linking; tags consume the result.

## 2. Core Concepts

### ArticleBundle

`ArticleBundle` is the object streamed through the system:

- `root`: one article or user post.
- `comments`: comments/replies attached to the root.
- `event_ids`: list of linked event ids attached to the root (zero or more).
- `customer`: compact customer context.
- `linked_events`: optional resolved event contexts (loaded from a sibling
  store keyed by `event_id`).

Events are never extracted or linked from comments — only from the root
article/post. Comments inherit the root's `event_ids` for stance context but
do not contribute claims by default.

A real raw document of this shape lives in
`data/ayuntamiento_qro/ayuntamiento_qro_20260506_175946.json`, produced by
`PoC/get_data.py`. The pre-linked fixture used by tags adds two fields per
document: `comments` (already present in the raw shape) and `event_ids`
(populated by the linking pre-pass — see §7).

### Stance

A stance is a typed public signal expressed by an item toward the customer or
the customer's actions. Stances are **not claims**. They describe perception,
requests, questions, gratitude, complaints, support, or noise.

An item can emit more than one stance assignment. Example:

`"Gracias por arreglar la calle, pero falta alumbrado en mi colonia."`

→ two assignments:

- `gratefulness`: `agradecimiento por mantenimiento vial`
- `suggestion`: `peticion de mas alumbrado publico`

Each assignment is independently typed and independently catalogued. For
catalog-bearing types, `stance_id` should point to the best matching entry
when one exists; otherwise it may be `null` and remain an uncatalogued typed
signal (evidence for later catalog growth).

### Stance Catalog

The stance catalog is per customer. It is physically one flat dictionary of
`StanceEntry` records, but logically it has one entry-set per catalog-bearing
stance type.

Catalog-bearing types (have entries, eligible for streaming growth):

- `entity_stance`
- `complaint`
- `gratefulness`
- `suggestion`
- `request`
- `denuncia`
- `question`
- `endorsement`

Tag-only types (assignment-only, never create catalog entries):

- `noise`

Every data item can emit more than one type. Each typed assignment can point to
a catalog entry or remain uncatalogued with `stance_id = null`. Uncatalogued
rows are first-class evidence for later catalog growth and consolidation.

### Claim

A claim is a factual assertion about a linked event. Claims are scoped to
`(customer_id, event_id)`. Claim extraction is **only** run for items that
have at least one linked event; items with no linked event do not call the
claim extractor.

Claims are extracted from articles/posts only by default, configurable via an
`include_comments` flag (default `False`).

## 3. Stance Types

### `entity_stance`

Durable quality or behavior attributed to the customer. Broadest perception
catalog.

Catalog label shape: `<subject> es/hace <quality>`.

| Text | Catalog label |
|---|---|
| "El ayuntamiento siempre se tarda meses con cualquier tramite." | `el ayuntamiento es ineficiente` |
| "Nunca contestan el telefono ni los mensajes." | `el ayuntamiento es inaccesible` |
| "No se sabe en que se gastan el dinero." | `el ayuntamiento es opaco con sus finanzas` |
| "Siempre me atienden bien y resuelven rapido." | `el ayuntamiento es eficiente y atento` |

Catalog: yes. Streaming growth: yes.

### `complaint`

Complaint about a concrete or recurring problem attributed to the customer.

Catalog label shape: `problemas en X`, `fallas en Y`, `demoras en Z`.

| Text | Catalog label |
|---|---|
| "Me cobraron mal otra vez el predial." | `problemas en cobros del predial` |
| "El telefono de atencion lleva dias caido." | `fallas en atencion telefonica` |
| "Llevo tres horas formado y no avanzan." | `demoras en atencion presencial` |
| "La calle lleva semanas sin alumbrado." | `fallas en alumbrado publico` |

Catalog: yes. Streaming growth: yes.

### `gratefulness`

Positive recognition or gratitude for an action, service, or effort.

Catalog label shape: `agradecimiento por X` or `reconocimiento a X`.

| Text | Catalog label |
|---|---|
| "Gracias por arreglar la calle tan rapido." | `agradecimiento por mantenimiento vial` |
| "Felicidades a los policias por el operativo." | `reconocimiento a la labor policial` |
| "Que bueno que ya recogen la basura a tiempo." | `reconocimiento al servicio de limpia` |

Catalog: yes. Streaming growth: yes.

### `suggestion`

Public proposal or recommended action directed at the customer.

Catalog label shape: `peticion de X`, `ampliacion de Y`, `modernizacion de Z`.

| Text | Catalog label |
|---|---|
| "Deberian poner topes en esa avenida." | `peticion de medidas de seguridad vial` |
| "Podrian ampliar el horario de las oficinas." | `ampliacion de horarios de atencion` |
| "Ya es hora de digitalizar los tramites." | `modernizacion de tramites digitales` |
| "Hace falta mas alumbrado en el centro." | `peticion de mas alumbrado publico` |

Catalog: yes. Streaming growth: yes. Distinguish from `request`:
suggestions are public/general, requests are personal/specific.

### `request`

Specific personal ask for attention, information, or help.

Catalog label shape: `solicitud de <personal help or service area>`.

| Text | Catalog label |
|---|---|
| "Podrian revisar mi expediente 1234?" | `solicitud de revision de expediente` |
| "Necesito ayuda con mi tramite del predial." | `solicitud de ayuda con tramite de predial` |
| "Atiendan mi reporte de fuga, lleva una semana." | `solicitud de atencion a reporte de fuga` |

Catalog: yes. Streaming growth: yes. Assignments may still be uncatalogued
when the ask is too unique or no current entry fits.

### `denuncia`

Public allegation of irregularity, abuse, illicit conduct, or misconduct.

Catalog label shape: `denuncias de <irregular conduct>`.

| Text | Catalog label |
|---|---|
| "Vi a un policia recibiendo dinero." | `denuncias de corrupcion policial` |
| "El contrato de obra esta claramente amanado." | `denuncias de contrataciones irregulares` |
| "Los inspectores piden mordida para no clausurar." | `denuncias de extorsion por inspectores` |
| "Usan camionetas oficiales para asuntos personales." | `denuncias de uso indebido de recursos publicos` |

Catalog: yes. Streaming growth: yes. High-risk type — should also feed
alerting/reporting.

### `question`

Open information-seeking question directed at or about the customer. The
catalog is an FAQ-topic catalog.

Catalog label shape: short topic label.

| Text | Catalog label |
|---|---|
| "Donde puedo pagar mi predial?" | `pago del predial` |
| "Que documentos necesito para el acta?" | `requisitos para acta de nacimiento` |
| "A que hora abren las oficinas?" | `horarios de atencion` |
| "Como saco la licencia de conducir?" | `tramite de licencia de conducir` |

Catalog: yes. Streaming growth: yes.

### `endorsement`

General support or rejection toward the customer, leader, administration, or
political project. Polarity is carried by the label itself (`apoyo a X` vs
`rechazo a X`), not by a separate sentiment field.

Catalog label shape: `apoyo a X` or `rechazo a X`.

| Text | Catalog label |
|---|---|
| "Vamos con FeliFer, todo el respaldo." | `apoyo al alcalde` |
| "No apoyo a este gobierno." | `rechazo al gobierno actual` |
| "Fuera el alcalde, ya estuvo." | `rechazo al alcalde` |

Catalog: yes. Streaming growth: yes. Two opposite entries (`apoyo al alcalde`
and `rechazo al alcalde`) typically coexist whenever the public is divided.

### `noise`

Greeting, spam, off-topic, promotion, emoji-only content, or content with no
signal about the customer.

| Text | Assignment |
|---|---|
| "Buenos dias a todos." | `noise`, `stance_id = null` |
| "Siganme en mi canal." | `noise`, `stance_id = null` |
| "🙏🙏🙏" | `noise`, `stance_id = null` |

Catalog: no. Mostly-noise items should receive exactly one `noise` assignment
and no other stance.

### Type Tie-Break

If one stance idea could fit multiple types, choose the most specific type:

`denuncia > request > complaint > suggestion > gratefulness > endorsement > entity_stance > question > noise`

This resolves ambiguity for one idea. It does not prevent one item from
emitting multiple distinct stance assignments.

## 4. Data Stream Design

### Target Stream Unit

The target stream processes one `ArticleBundle` at a time:

```text
ArticleBundle
  root: SourceItem(article | user_post)
  comments: list[SourceItem(user_comment)]
  event_ids: list[str]
  linked_events: list[LinkedEventContext]   # resolved from event_ids
  customer: Customer
```

### Event Context

Event context is optional per item. When present, prompt context should
include only:

```json
{"description": "..."}
```

No prompt should send full linked event records, source ids, geocoding
details, or merge metadata unless the specific decision requires them.

### Processing Order

For each `ArticleBundle`:

1. Load root article/post, comments, and `event_ids`.
2. Resolve `event_ids` to compact `LinkedEventContext` blocks.
3. Run stance type triage over the root and comments.
4. Run per-type stance tagging against current catalog slices.
5. Apply stance assignments and allowed catalog updates.
6. If `event_ids` is non-empty, run claim extraction once per
   `(root, event_id)`, attaching that event's existing claim clusters as
   guidance. Items without `event_ids` skip this step entirely.
7. Group/update claims under each linked event.
8. Periodically run consolidation over accumulated assignments, catalog
   entries, raw claims, and clusters (see §5.7).

## 5. Pipelines

### 5.1 Bootstrap Pipeline

Purpose: build an initial per-customer catalog for every catalog-bearing
stance type.

Inputs:

- customer `name` and `description`,
- a sampled customer corpus (`ArticleBundle` objects),
- optional linked event descriptions only as weak context.

Steps:

1. Build the corpus.
2. Run the same batched `TypeTriageStep` used by streaming.
3. Drop `noise`. (Tag-only types create no entries.)
4. Group all triaged occurrences by stance type.
5. For each catalog-bearing stance type, run **one** per-type catalog
   bootstrap LLM call with that type's full set of occurrences. Output: the
   type's entry-set.
6. Validate each proposed entry:
   - non-empty label,
   - active `primary_type`,
   - at least `min_evidence` valid local evidence ids,
   - max entries per type.
7. Add entries to the flat `StanceCatalog` with `primary_type`.

Defaults:

- `min_evidence = 2`,
- target entries per type: 3 to 15,
- tag-only types create no entries.

### 5.2 Type Triage Pipeline

Purpose: classify which stance-type ideas exist in each item. It does not
choose catalog entries and does not extract claims.

Inputs:

- compact customer block: `name`, `description`,
- optional event block: `description`,
- compact items: local integer `id`, `kind`, `text`.

Output: one row per stance idea. An item with two distinct ideas produces two
rows; tie-break (§3) picks the type for each idea independently.

```json
{
  "triage": [
    {
      "source_item_id": 1,
      "stance_type": "complaint",
      "brief_summary": "...",
      "importance_hint": "medium"
    },
    {
      "source_item_id": 1,
      "stance_type": "entity_stance",
      "brief_summary": "...",
      "importance_hint": "low"
    }
  ]
}
```

Parser rules:

- Map local ids back to canonical `SourceItem.id`.
- Drop unknown ids.
- Each row carries exactly one `stance_type` (post tie-break).
- Allow multiple rows per `source_item_id` — one per distinct idea.
- If `noise` is emitted for an item, keep only one `noise` row and drop any
  other row for that item.
- Soft cap: 4 rows per item.

### 5.3 Stance Tagging Pipeline

Purpose: map triaged stance ideas to existing catalog entries, and propose
additions or updates where allowed.

Inputs:

- active stance type,
- compact customer block,
- optional event description,
- compact items with local ids,
- triage hints for candidate items,
- catalog slice for the active type only.

Output:

- `StanceAssignment` rows,
- `StanceProposal` rows for additions/renames.

Rules:

- One LLM call per **active** stance-bearing type. *Active* = the triage
  produced ≥1 row of that type. Types with no triage rows are skipped — no
  call is made.
- Tag-only types (`noise`) do not enter catalog tagging.
- The catalog payload includes only entries for that type.
- Assignments must match the active type.
- Assignments may have `stance_id = null` for any catalog-bearing type when no
  current entry fits. These uncatalogued rows are evidence for consolidation.
- `noise` assignments are emitted directly from triage with `stance_id = null`.
- Streaming `add` and `rename` proposals are accepted for **all
  catalog-bearing types**.

### 5.4 Stance Update Pipeline

Purpose: apply stance assignments and catalog mutations from §5.3.

**No adjudicator LLM call.** The updater applies the tagger's decisions
deterministically based on validation rules. Catalog drift in streaming is
contained by the periodic consistency pass (§5.7), which is the place where
batch hygiene happens. This keeps the streaming hot path simple and cheap.

Inputs:

- current `StanceCatalog`,
- assignments,
- proposals.

Actions (deterministic, no LLM):

- `assign` — append the assignment to the catalog after passing validation.
- `add` — create a new `StanceEntry` with the proposal's `primary_type`,
  label, description; route the originating assignments to the new entry.
- `rename` — rewrite an existing entry's label/description (id stable; old
  label appended to `aliases`).
- `reject` — drop the assignment or proposal because validation failed.

Validation rules (these are when `reject` fires):

- assignment's `stance_type` must match the target entry's `primary_type`,
- tag-only assignments only allowed for `noise`,
- uncatalogued assignments (`stance_id = null`) allowed for every
  catalog-bearing type — these rows are first-class evidence for the
  consistency pass,
- `add` proposals must declare a `primary_type ∈ STANCE_BEARING_TYPES`,
- `rename` proposals must reference an `existing_id` in the catalog.

Renames are id-stable: existing assignments keep pointing at the same id and
follow the new label transparently.

### 5.5 Claim Extraction Pipeline

Purpose: extract **raw** factual claims from items linked to events. This step
does NOT route claims into clusters — that's §5.6's job.

Inputs:

- compact customer block: `entity_id`, `name`, `description`,
- one linked event description (call is per `(root, event_id)`),
- existing claim clusters for that event (canonical text only) as
  **de-duplication awareness** — the model can avoid re-extracting verbatims
  it can see are already represented, but it does not assign cluster ids,
- root article/post item by default; comments included only when
  `include_comments = True` (default `False`),
- local integer ids.

Output:

```json
{
  "claims": [
    {
      "source_item_id": 1,
      "verbatim": "...",
      "importance": 1,
      "importance_reason": "..."
    }
  ]
}
```

The customer is implicit because the call is scoped per `(customer, event)`;
there is no `affected_entity_ids` field — every emitted claim affects the
fixed customer.

Importance rubric:

- `1` — low: factual but tangential to the customer's interests.
- `2` — medium: relevant fact, customer should be aware.
- `3` — high: material allegation or operational impact (often `denuncia`-
  adjacent).

Rules:

- Items with no linked events skip this pipeline entirely (no LLM call).
- For multi-event roots, call once per `(root, event_id)` with that event's
  cluster catalog passed for de-dup awareness.
- Comments are excluded by default; toggle via `include_comments`.
- Unknown local item ids are dropped.
- The output is a flat list of `RawClaim` rows; clustering decisions happen
  in §5.6.

### 5.6 Claim Grouping Pipeline

Purpose: maintain per-event canonical claim clusters.

Scope: `(customer_id, event_id)`.

Inputs:

- event description,
- existing clusters for that event (canonical claim text),
- incoming raw claims with local claim indices.

Actions: assign / create / rename / merge / drop.

Rules:

- Cluster ids are canonical ids, not local prompt ids.
- Raw claim references use local `claim_index` in prompts.
- Cluster memberships preserve original source item ids after parsing.

### 5.7 Consistency Pass

Purpose: periodic global re-evaluation of the catalog to clean drift the
streaming hot path can't see.

Cadence: every `N` source threads, on a `T`-day floor, or on demand.

Inputs:

- stance catalog entries,
- a stratified sample of recent assignments (each already carries
  `stance_type` from streaming — the pass does NOT re-triage),
- compact source item samples,
- claim summaries by event.

Steps:

1. Sample assignments stratified by `stance_type` (each catalog-bearing
   type gets a slot; uncatalogued `stance_id = null` rows are over-sampled
   relative to their share, since they're the main growth signal).
2. Group the sample by `assignment.stance_type` (no re-triage — the type is
   already there).
3. Per type, run one consolidation LLM call producing the actions below.
4. Apply mutations through `StanceCatalog` methods directly (no second LLM
   call to adjudicate — like §5.4, the consistency pass owns its own
   decisions and validation).

Actions:

- add entries for recurring patterns the streaming missed (typically
  evidenced by clusters of `stance_id = null` assignments of one type),
- rename unclear labels,
- merge duplicate entries (intra-type only),
- retire stale or bad entries,
- reroute assignments from one entry to another.

Rules:

- One consolidation call per active stance-bearing type.
- Never merge entries across different `primary_type`.
- `noise` stays assignment-only.
- Validation mirrors §5.4 (deterministic — no adjudicator LLM).

## 6. Prompting Rules

### Compact Context

Send only fields required for the decision.

Customer context:

```json
{"name": "...", "description": "..."}
```

Claim extraction may additionally include:

```json
{"entity_id": 75, "name": "...", "description": "..."}
```

Event context:

```json
{"description": "..."}
```

Source items:

```json
{"id": 1, "kind": "user_comment", "text": "..."}
```

Never send full source URLs, full linked event records, customer aliases or
types, geocoding records, merge metadata, or raw database records unless a
prompt needs that exact field.

### Local Id Mapping

Every prompt call builds local ids starting at 1.

- Local ids exist only inside one LLM call.
- Prompts use integers only.
- Parsers map integers back to canonical `SourceItem.id`.
- Unknown local ids are dropped and counted.
- Local ids are not stored in catalog state.

### Batching

All high-volume prompts batch inputs. Bootstrap per-type calls are the
exception (see §5.1 step 5 — single-shot, full type-occurrence set).

Default batch size: `15` comments/items per call unless configured otherwise.

Root context rule:

- If batching comments from a post/article, include the root in every
  comment batch as context, not as a candidate.
- Triage candidates: roots in their own call; comments only in their batch.
- Stance tagging candidates: those `triage_hints` flag for the active type.
- Claim extraction candidates: roots only by default.

### Prompt Separation

Do not combine unrelated tasks.

- Type triage does not pick catalog entries.
- Stance tagging does not extract claims.
- Claim extraction does not update clusters.
- Updaters adjudicate mutations, not raw extraction.
- Consolidation handles cross-batch catalog hygiene.

### Output Validation

Every parser validates LLM output before mutating state.

- required fields present,
- local ids known,
- stance type valid,
- entry type matches assignment type,
- tag-only rows have `stance_id = null`,
- uncatalogued catalog-bearing rows may also have `stance_id = null`,
- evidence count meets minimums,
- text fields are non-empty where required.

## 7. Test Fixtures & Local Step-by-Step Run

The pipeline runs end-to-end against local files with no live services.

### 7.1 Inputs

- Raw documents like `data/ayuntamiento_qro/ayuntamiento_qro_20260506_175946.json`
  (output of `PoC/get_data.py`). Each document already carries `comments`.
- A customer fixture (`data/tags/customer_<entity_id>.json`) built by the
  existing `scripts/build_customer_fixture.py`. The fixture mirrors the kgdb
  `entities` row + joined helpers (types / locations / aliases) and matches
  the `Customer` dataclass in [`data_model.md`](data_model.md). Regenerate by
  re-running the script when the kgdb row changes.

### 7.2 Pre-link pass (separate script)

A script `scripts/build_linked_fixture.py` runs:

1. Load raw documents.
2. Run extraction over each root.
3. Run linking over the extracted records.
4. Write two sibling files under `data/linked/`:
   - `data/linked/<source>.json` — the documents, each enriched with
     `event_ids: list[str]` next to `comments`. The shape stays compatible
     with the raw document; only `event_ids` is added.
   - `data/linked/<source>__events.json` — a flat dict keyed by `event_id`,
     each value `{description: str, event_type: str, name: str}` (the
     compact `LinkedEventContext` shape). This is what tags loads to
     resolve `event_ids` into the prompt-context blocks defined in §4.

This isolates the tags pipeline from extraction/linking — tags consumes the
`event_ids` field directly and reads the event descriptions from the sibling
event file.

### 7.3 Bootstrap

```bash
python -m src.entities.tags.bootstrap --customer 75 \
  --corpus data/linked/<file>.json
```

Outputs the typed catalog snapshot to `data/tags/customer_75/bootstrap.json`.

### 7.4 Streaming

```bash
python -m src.entities.tags.run \
  --customer 75 \
  --corpus data/linked/<file>.json \
  --catalog data/tags/customer_75/bootstrap.json
```

Per-`ArticleBundle` stdout snapshot + a final dump to
`data/tags/customer_75/run_<ts>.json`.

### 7.5 Consistency pass (manual)

```bash
python -m src.entities.tags.consistency --customer 75 \
  --catalog data/tags/customer_75/run_<ts>.json
```

Outputs a consistency-pass result alongside the run snapshot.

### 7.6 Inspecting snapshots

The snapshot file has two top-level keys:

- `stance_catalog` — typed entries + assignments.
- `claim_catalogs` — per-event clusters + assignments.

Each step also emits a per-call cache directory under
`cache/tags_<phase>/customer_<id>/<sha256>.json` so re-runs are deterministic
and don't re-bill.

## 8. Stage 2 — Database Coupling

The in-memory model maps onto kgdb tables; the runtime swap is a loader/saver
change with no consumer-side rewrites.

| In-memory class | Stage-2 destination |
|---|---|
| `Customer` | `kgdb.entities` + joined helpers |
| `StanceEntry` | `kgdb.stance_entries` (per customer) |
| `StanceAssignment` | `kgdb.stance_assignments` |
| `RawClaim` | `kgdb.raw_claims` (per `event_id`) |
| `ClaimCluster` | `kgdb.claim_clusters` (per `(customer_id, event_id)`) |
| `ClaimAssignment` | `kgdb.claim_assignments` |
| `ConsistencyPassResult` | `kgdb.consistency_pass_runs` (audit) |

Schema specification and column-level mapping live in
`media-backend-paid/docs/DATABASE_POSTGRES.md`. The Stage-2 swap replaces
`load_*_from_json` / `save_snapshot` with `load_*_from_db` / `write_back`
implementations behind the existing in-memory APIs.

## 9. Implementation Direction

The target service split:

1. `ArticleBundleRetriever` — emits `ArticleBundle`s with comments and
   pre-linked `event_ids`.
2. `TypeTriageStep` — batched type classification over root and comments.
3. `StanceTagger` — per-type catalog assignment.
4. `StanceUpdater` — apply assignments and streaming-safe catalog changes.
5. `ClaimTagger` — extract claims from items with linked events.
6. `ClaimUpdater` — group claims by linked event.
7. `ConsistencyPassStep` — periodic cross-thread consolidation.

The `StreamingTagsPipeline` is driven by `ArticleBundle`s, not events. Linked
events are attached context and claim scope. Stances are always
customer-catalog assignments over source items, optionally filterable by
`event_id`.

For dataclass shapes and field-level details, see
[`data_model.md`](data_model.md).
