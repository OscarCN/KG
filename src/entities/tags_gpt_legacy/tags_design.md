# Tags GPT Design Specification

## 1. Purpose

`tags_gpt` is the decoupled tags system for customer-facing media analysis. It
turns article/post threads into:

- typed stance assignments over a per-customer stance catalog,
- per-event factual claim clusters,
- periodic catalog updates and consolidation proposals.

The primary stream unit is an **article or social post with its comments**. Linked
events are context when present, not the primary unit of execution. A source item
may be attached to zero, one, or more linked event ids from local retrieval or a
database lookup. Event context helps scope claims and interpret text, but stance
catalogs remain customer-wide.

## 2. Core Concepts

### Source Thread

A source thread is the object streamed through the system:

- `root`: one article or user post.
- `comments`: comments/replies attached to the root.
- `customer`: compact customer context.
- `linked_events`: optional linked event contexts attached to the root.
- `source_ids`: original external ids, kept outside prompts where possible.

The root and its comments are the primary tagging elements. Linked events are
read-only context and are used mainly for claim scope, reports, and event filters.

### Stance

A stance is a typed public signal expressed by an item toward the customer or the
customer's actions. Stances are **not claims**. Stances describe perception,
requests, questions, gratitude, complaints, support, or noise.

An item can emit more than one stance assignment. Example:

`"Gracias por arreglar la calle, pero falta alumbrado en mi colonia."`

This should produce:

- `gratefulness`: `agradecimiento por mantenimiento vial`
- `suggestion`: `peticion de mas alumbrado publico`

Each assignment is independently typed and independently catalogued. For
catalog-bearing types, `stance_id` should point to the best matching entry when
one exists; otherwise it may be `null` and remain an uncatalogued typed signal.

### Stance Catalog

The stance catalog is per customer. It is physically one flat dictionary of
`StanceEntry` records, but logically it has one entry set per catalog-bearing
stance type.

Catalog-bearing types:

- `entity_stance`
- `complaint`
- `gratefulness`
- `suggestion`
- `request`
- `denuncia`
- `question`

Tag-only types:

- `endorsement`
- `noise`

Every data item can emit more than one type. Each typed assignment can point to
a catalog entry or remain uncatalogued with `stance_id = null`. Uncatalogued rows
are first-class evidence for later catalog growth and consolidation.

Tag-only types always have `stance_id = null` and never create catalog entries.

### Claim

A claim is a factual assertion about a linked event or event-like situation. Claims
are scoped to `(customer_id, event_id)` when an event is present. If no linked
event exists, claims may be dropped, held as unscoped raw claims, or routed to a
future event-discovery/linking step. In v1, claim extraction is primarily from
articles/posts, not comments.

## 3. Stance Types

### `entity_stance`

Durable quality or behavior attributed to the customer. This is the broadest
perception catalog.

Catalog label shape: `<subject> es/hace <quality>`.

Examples:

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

Examples:

| Text | Catalog label |
|---|---|
| "Me cobraron mal otra vez el predial." | `problemas en cobros del predial` |
| "El telefono de atencion lleva dias caido." | `fallas en atencion telefonica` |
| "Llevo tres horas formado y no avanzan." | `demoras en atencion presencial` |
| "La calle lleva semanas sin alumbrado." | `fallas en alumbrado publico` |

Catalog: yes. Streaming growth: no by default. New entries are created by
bootstrap or consolidation.

### `gratefulness`

Positive recognition or gratitude for an action, service, or effort.

Catalog label shape: `agradecimiento por X` or `reconocimiento a X`.

Examples:

| Text | Catalog label |
|---|---|
| "Gracias por arreglar la calle tan rapido." | `agradecimiento por mantenimiento vial` |
| "Felicidades a los policias por el operativo." | `reconocimiento a la labor policial` |
| "Que bueno que ya recogen la basura a tiempo." | `reconocimiento al servicio de limpia` |
| "Gracias por la jornada de salud gratuita." | `agradecimiento por jornadas de salud` |

Catalog: yes. Default sentiment: positive.

### `suggestion`

Public proposal or recommended action directed at the customer.

Catalog label shape: `peticion de X`, `ampliacion de Y`,
`modernizacion de Z`.

Examples:

| Text | Catalog label |
|---|---|
| "Deberian poner topes en esa avenida." | `peticion de medidas de seguridad vial` |
| "Podrian ampliar el horario de las oficinas." | `ampliacion de horarios de atencion` |
| "Ya es hora de digitalizar los tramites." | `modernizacion de tramites digitales` |
| "Hace falta mas alumbrado en el centro." | `peticion de mas alumbrado publico` |

Catalog: yes. Distinguish from `request`: suggestions are public/general,
requests are personal/specific.

### `request`

Specific personal ask for attention, information, or help.

Catalog label shape: `solicitud de <personal help or service area>`.

Examples:

| Text | Catalog label |
|---|---|
| "Podrian revisar mi expediente 1234?" | `solicitud de revision de expediente` |
| "Necesito ayuda con mi tramite del predial." | `solicitud de ayuda con tramite de predial` |
| "Atiendan mi reporte de fuga, lleva una semana." | `solicitud de atencion a reporte de fuga` |
| "Me pueden mandar el folio de mi pago?" | `solicitud de informacion de pagos` |

Catalog: yes. Assignments may still be uncatalogued when the ask is too unique
or no current request entry fits. Useful for service volume and unresolved
personal asks.

### `denuncia`

Public allegation of irregularity, abuse, illicit conduct, or misconduct.

Catalog label shape: `denuncias de <irregular conduct>`.

Examples:

| Text | Catalog label |
|---|---|
| "Vi a un policia recibiendo dinero." | `denuncias de corrupcion policial` |
| "El contrato de obra esta claramente amanado." | `denuncias de contrataciones irregulares` |
| "Los inspectores piden mordida para no clausurar." | `denuncias de extorsion por inspectores` |
| "Usan camionetas oficiales para asuntos personales." | `denuncias de uso indebido de recursos publicos` |

Catalog: yes. Default sentiment: negative. This type is high-risk and should
also feed alerting/reporting.

### `question`

Open information-seeking question directed at or about the customer. The catalog
is an FAQ-topic catalog.

Catalog label shape: short topic label.

Examples:

| Text | Catalog label |
|---|---|
| "Donde puedo pagar mi predial?" | `pago del predial` |
| "Que documentos necesito para el acta?" | `requisitos para acta de nacimiento` |
| "A que hora abren las oficinas?" | `horarios de atencion` |
| "Como saco la licencia de conducir?" | `tramite de licencia de conducir` |

Catalog: yes. Default sentiment: neutral unless frustration is explicit.

### `endorsement`

General support or rejection toward the customer, leader, administration, or
political project.

Catalog label: none.

Examples:

| Text | Assignment | Sentiment |
|---|---|---|
| "Vamos con FeliFer, todo el respaldo." | `endorsement`, `stance_id = null` | positive |
| "Sigan asi, ayuntamiento." | `endorsement`, `stance_id = null` | positive |
| "No apoyo a este gobierno." | `endorsement`, `stance_id = null` | negative |
| "Fuera el alcalde, ya estuvo." | `endorsement`, `stance_id = null` | negative |

Catalog: no. Sentiment is required and meaningful. Aggregate endorsement by
sentiment, source thread, event context, and time window rather than catalog
entry.

### `noise`

Greeting, spam, off-topic, promotion, emoji-only content, or content with no
signal about the customer.

Catalog label: none.

Examples:

| Text | Assignment |
|---|---|
| "Buenos dias a todos." | `noise`, `stance_id = null` |
| "Siganme en mi canal." | `noise`, `stance_id = null` |
| "Jajaja que loco." | `noise`, `stance_id = null` |
| "..." | `noise`, `stance_id = null` |

Catalog: no. Mostly-noise items should receive exactly one `noise` assignment
and no other stance.

### Type Tie-Break

If one stance idea could fit multiple types, choose the most specific type:

`denuncia > request > complaint > suggestion > gratefulness > endorsement > entity_stance > question > noise`

This resolves ambiguity for one idea. It does not prevent one item from emitting
multiple distinct stance assignments.

## 4. Data Stream Design

### Target Stream Unit

The target stream processes one `SourceThread` at a time:

```text
SourceThread
  root: SourceItem(article | user_post)
  comments: list[SourceItem(user_comment)]
  linked_events: list[LinkedEventContext]
  customer: Customer
```

The root and comments are the primary units for tagging. Each assignment and raw
claim references a `source_item_id`. Linked event ids are attached metadata:

- from local fixture retrieval in manual runs,
- from database lookup in production,
- from the event linker when extraction/linking is run in the same job.

### Event Context

Event context is optional. When present, prompt context should include only:

```json
{"description": "..."}
```

No prompt should send full linked event records, source ids, geocoding details,
or merge metadata unless the specific decision requires them.

### Processing Order

For each source thread:

1. Load root article/post and comments.
2. Attach linked event contexts, if available.
3. Run stance type triage over the root and comments.
4. Run per-type stance tagging against current catalog slices.
5. Apply stance assignments and allowed catalog updates.
6. Run claim extraction from the root article/post only by default.
7. Group/update claims under each linked event context, if present.
8. Periodically run consolidation over accumulated assignments, catalog entries,
   raw claims, and clusters.

## 5. Pipelines

### 5.1 Bootstrap Pipeline

Purpose: build an initial per-customer catalog for every catalog-bearing stance
type.

Inputs:

- customer `name` and `description`,
- article/post threads or a sampled customer corpus,
- optional linked event descriptions only as weak context.

Steps:

1. Build a corpus of source threads.
2. Run the same batched `TypeTriageStep` used by streaming.
3. Drop `endorsement` and `noise` because they are tag-only.
4. Group triaged items by catalog-bearing stance type.
5. For each type, run a per-type catalog bootstrap prompt.
6. Validate each proposed entry:
   - non-empty label,
   - active `primary_type`,
   - at least `min_evidence` valid local evidence ids,
   - max entries per type.
7. Add entries to the flat `StanceCatalog` with `primary_type`.

Output:

- `StanceCatalog` with typed `StanceEntry` records.

Defaults:

- `min_evidence = 2`
- target `entity_stance`: 5 to 15 entries
- target other catalog-bearing types: 0 to 10 entries
- tag-only types create no entries

### 5.2 Type Triage Pipeline

Purpose: classify which stance-type ideas exist in each item. It does not choose
catalog entries and does not extract claims.

Inputs:

- compact customer block: `name`, `description`,
- optional event block: `description`,
- compact items: local integer `id`, `kind`, `text`.

Output:

```json
{
  "triage": [
    {
      "source_item_id": 1,
      "stance_type": "complaint",
      "importance_hint": "medium"
    }
  ]
}
```

Parser rules:

- Map local ids back to canonical `SourceItem.id`.
- Drop unknown ids.
- Allow multiple types per item.
- If `noise` is emitted for an item, keep only one `noise` row for that item.
- Limit to a small number of stance ideas per item, default 4.

### 5.3 Stance Tagging Pipeline

Purpose: map triaged stance ideas to existing catalog entries, and optionally
propose additions where allowed.

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

- One LLM call per active stance-bearing type. Tag-only types do not enter
  catalog tagging.
- The catalog payload includes only entries for that type.
- Assignments must match the active type.
- Assignments may have `stance_id = null` for any type when no current entry
  fits or when the type is tag-only. These uncatalogued rows are evidence for
  consolidation.
- `endorsement` and `noise` assignments are emitted directly from triage with
  `stance_id = null`.
- Streaming add proposals are accepted only for `entity_stance` by default.

### 5.4 Stance Update Pipeline

Purpose: apply stance assignments and adjudicate catalog mutations.

Inputs:

- current `StanceCatalog`,
- assignments,
- proposals,
- compact evidence samples.

Actions:

- assign,
- add,
- rename,
- reject.

Rules:

- Reject assignment if `stance_type` does not match the target entry's
  `primary_type`.
- Allow tag-only assignments only for `endorsement` and `noise`.
- Allow uncatalogued assignments with `stance_id = null` for every non-noise
  type; for catalog-bearing types these rows should be sampled by consolidation.
- For streaming updates, block non-`entity_stance` catalog growth by default.
- Preserve assignment history when entries are renamed or merged later.

### 5.5 Claim Extraction Pipeline

Purpose: extract factual claims from article/post roots.

Inputs:

- compact customer block: `entity_id`, `name`, `description`,
- optional event description,
- root article/post items only by default,
- local integer ids.

Output:

```json
{
  "claims": [
    {
      "source_item_id": 1,
      "affected_entity_ids": [75],
      "verbatim": "...",
      "importance": 1,
      "importance_reason": "..."
    }
  ]
}
```

Rules:

- Comments are excluded by default.
- Unknown local item ids are dropped.
- Claims must affect the customer or a configured related entity.
- If no linked event context exists, do not create a normal event claim cluster
  in v1.

### 5.6 Claim Grouping Pipeline

Purpose: maintain per-event canonical claim clusters.

Scope:

- `(customer_id, event_id)`

Inputs:

- event description,
- existing clusters for that event,
- incoming raw claims with local claim indices.

Actions:

- assign raw claim to an existing cluster,
- create a new cluster,
- rename a cluster,
- merge clusters,
- drop unsupported or irrelevant claims.

Rules:

- Cluster ids are canonical ids, not local prompt ids.
- Raw claim references use local `claim_index` in prompts.
- Cluster memberships preserve original source item ids after parsing.

### 5.7 Consolidation Pipeline

Purpose: periodically clean and grow catalogs after enough evidence accumulates.

Run cadence:

- every `N` source threads,
- at the end of a batch job,
- or on demand in manual/debug runs.

Inputs:

- stance catalog entries,
- recent and high-signal stance assignments,
- uncatalogued catalog-bearing assignments,
- claim summaries by event,
- compact source item samples.

Actions:

- add entries for recurring non-`entity_stance` patterns,
- rename unclear labels,
- merge duplicate entries,
- retire stale or bad entries,
- reroute assignments from one entry to another,
- propose secondary entries derived from repeated claims or questions.

Rules:

- Prefer one consolidation call per stance type.
- Never merge entries across different `primary_type`.
- `endorsement` and `noise` stay assignment-only.
- Uncatalogued `request`, `complaint`, `question`, and other catalog-bearing rows
  are eligible evidence for new entries.
- Claim data can suggest stance entries, but stance entries do not become claim
  clusters.

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

Never send full source URLs, full linked event records, customer aliases/types,
geocoding records, merge metadata, or raw database records unless a prompt needs
that exact field.

### Local Id Mapping

Every prompt call builds local ids starting at 1.

Rules:

- local ids exist only inside one LLM call,
- prompts use integers only,
- parsers map integers back to canonical `SourceItem.id`,
- unknown local ids are dropped and counted,
- local ids are not stored in catalog state.

### Batching

All high-volume prompts must batch inputs.

Default batch size:

- `12` comments/items per call unless configured otherwise.

Root context rule:

- If batching comments from a post/article, include the root post/article in
  every comment batch.
- The root is context in comment batches, not a candidate output unless the call
  is explicitly tagging the root.
- If a post has 100 comments and batch size is 12, make 9 comment calls. Each
  call includes the same root and one comment chunk.

Candidate rules:

- For triage, root/article/post items are candidates in their own call.
- Comments are candidates only in their comment chunk call.
- For stance tagging, `triage_hints` define the candidate items; repeated root
  context should not generate duplicate assignments.
- For claim extraction, only root article/post candidates are sent by default.

### Prompt Separation

Do not combine unrelated tasks.

- Type triage does not pick catalog entries.
- Stance tagging does not extract claims.
- Claim extraction does not update clusters.
- Updaters adjudicate mutations, not raw extraction.
- Consolidation handles cross-batch catalog hygiene.

### Output Validation

Every parser validates LLM output before mutating state.

Common validation:

- required fields present,
- local ids known,
- stance type valid,
- entry type matches assignment type,
- tag-only rows have `stance_id = null`,
- uncatalogued catalog-bearing rows may also have `stance_id = null`,
- evidence count meets minimums,
- text fields are non-empty where required.

## 7. Data Model Summary

### StanceEntry

```text
id: str
label: str
description: str
primary_type: StanceType
created_at: str
aliases: list[str]
```

### StanceAssignment

```text
source_item_id: str
source_kind: article | user_post | user_comment
customer_id: int
stance_id: str | null
stance_type: StanceType
sentiment: positive | negative | neutral | null
consistency_relevance: low | medium | high | null
event_id: str | null
reason: str
```

### RawClaim

```text
event_id: str
customer_id: int
affected_entity_ids: list[int]
verbatim: str
source_item_id: str
source_kind: article | user_post | user_comment
importance: 1 | 2 | 3
importance_reason: str
```

### ClaimCluster

```text
id: str
customer_id: int
event_id: str
canonical: str
members: list[RawClaim]
aliases: list[str]
importance_max: int
```

## 8. Persistence and Debugging

### Persistence

The current implementation is JSON/in-memory.

Durable persistence should store:

- source thread metadata,
- source items,
- linked event contexts,
- stance entries,
- stance assignments,
- claim clusters,
- claim assignments,
- consolidation decisions.

### Debugging

The LLM layer should log every prompt and JSON response under a dedicated logger:

```text
src.entities.tags_gpt.llm_io
```

Manual runners should suppress dependency logs and enable this logger only when
debugging prompt behavior.

Debug snapshots should include:

- typed stance catalog,
- assignments by item,
- claim clusters by linked event,
- prompt-call counters,
- dropped-output counters.

## 9. Implementation Direction

The target service split is:

1. `SourceThreadRetriever`: emits article/post threads with comments and optional
   linked event contexts.
2. `TypeTriageStep`: batched type classification over root and comments.
3. `StanceTagger`: per-type catalog assignment.
4. `StanceUpdater`: apply assignments and streaming-safe catalog changes.
5. `ClaimTagger`: extract claims from roots.
6. `ClaimUpdater`: group claims by linked event.
7. `ConsistencyPassStep`: periodic cross-thread consolidation.

The most important stream change is that `StreamingTagsPipeline` should no longer
be driven by linked events. It should be driven by source threads. Linked events
are attached context and claim scope. Stances are always customer-catalog
assignments over source items, optionally filterable by event id.
