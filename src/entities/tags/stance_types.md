# Stance Types & Catalog Consistency

> **Status:** design only. No code yet. This file captures the taxonomy of stance types, the data model additions required to support them, the periodic consistency pass that keeps the catalog from drifting, and a backlog of related ideas.

## 1. Why types

Today every `StanceAssignment` carries a single `stance_id` referring to a `StanceEntry` and an implicit assumption that the utterance shape is "durable quality of the customer". In practice, the corpus is full of utterances that don't fit that mold — questions, requests, gratitude, mockery, greetings — and silently squeezing all of them into the same catalog is the main source of catalog drift, false-positive assignments, and noise in downstream reports.

Adding a `stance_type` field on the assignment lets us:

- segment dashboards/reports by utterance shape (don't mix grievances into the durable-quality distribution),
- run different aggregation rules per type (entity_stance is durable; complaint is event-bound),
- drop true noise (`greetings`, `off_topic`) deterministically instead of via heuristics,
- sharpen prompts later (per-type tagging if quality demands it),
- surface comms gaps (unanswered `question` count is a metric on its own).

## 2. Taxonomy (v1)

Nine types. Each item can carry **one or more stance assignments**, each one independently typed (see §2.1). The catalog has one entry-set per stance-bearing type (see §2.5) — entries within each type share the same semantic shape, distinct from the shapes of the other types. Claims are *not* a stance type; they're a parallel pipeline (see §2.4 for why).

### 2.1 Multi-stance per item

A single comment, post, or article can express several distinct ideas at once. Examples:

> *"el ayuntamiento es ineficiente, deberían modernizar sus sistemas"*
> → **two assignments:** `entity_stance` ("el ayuntamiento es ineficiente") + `suggestion` ("modernización de sistemas administrativos").

> *"gracias por arreglar la calle pero falta también la de Madero"*
> → **two assignments:** `gratefulness` ("agradecimiento por mantenimiento vial") + `suggestion` ("petición de mantenimiento en otras calles").

> *"vamos con FeliFer pero deberían bajar el predial"*
> → **two assignments:** `endorsement` (positive, "apoyo al alcalde") + `suggestion` ("petición de reducción de impuestos").

> *"me cobraron mal otra vez y nadie contesta el teléfono"*
> → **two assignments:** `complaint` ("problemas en cobros") + `complaint` ("fallas en atención telefónica"). Same type, two distinct stances.

Rules:

- An item can have **0, 1, or more** stance assignments.
- Each assignment carries exactly **one** type. Types are disjoint *per assignment*, not *per item*.
- Tag-only types (`request`, `noise`) coexist with stance-bearing assignments only when the LLM identifies clearly separable parts of the same utterance. For mostly-noise items with a fleeting stance hint, prefer a single `noise` assignment over a borderline stance.
- An item entirely composed of greetings/off-topic/promotional/call-to-action gets **exactly one** `noise` assignment (not multiple noise tags).

### 2.2 The nine types

For each type below: a short description, a few **real comments** (the kind of thing a user would actually post), the **stance label** that goes in the catalog when the comment is generalized into a recurring pattern, and notes on per-type behavior. The "real comment → stance label" pattern is what every prompt should be teaching the LLM.

---

#### `entity_stance` — cualidad o comportamiento DURADERO del cliente

The original notion: a quality the public attributes to the customer that holds across multiple events.

| Real comment | Stance label (what goes in the catalog) |
|---|---|
| "el ayuntamiento siempre se tarda meses con cualquier trámite" | "el ayuntamiento es ineficiente" |
| "FeliFer dijo una cosa el lunes y otra el viernes, ya no le creo" | "el alcalde es deshonesto / poco confiable" |
| "no contestan nunca, no tienen redes activas, nada" | "el ayuntamiento es inaccesible" |
| "siempre que voy me atienden súper bien, todo en tiempo" | "el ayuntamiento es eficiente y atento" |
| "están descuidando todas las calles del centro" | "el ayuntamiento descuida la infraestructura" |

**Notes.** Catalog entries are phrased as `"<sujeto> es/hace <cualidad>"`. Streaming Phase 2 can propose new entry adds for this type. Sentiment is implicit in the label ("ineficiente" = negative; "eficiente y atento" = positive) but `sentiment` is still emitted as an explicit field for downstream filtering.

---

#### `complaint` — queja, generalmente sobre un evento o incidente concreto

The user is complaining about something specific. The catalog entry is the **recurring complaint pattern** — the generalization, not the incident itself.

| Real comment | Stance label (what goes in the catalog) |
|---|---|
| "me cobraron mal otra vez en el predial" | "problemas en cobros" |
| "tienen el teléfono caído todo el día" | "fallas en atención telefónica" |
| "llevo 3 horas esperando que me atiendan" | "demoras en atención presencial" |
| "me cancelaron la cita sin avisar" | "cancelaciones sin aviso" |
| "el alumbrado lleva un mes apagado en mi colonia" | "fallas en alumbrado público" |

**Notes.** Entries are phrased as `"<sustantivo de problema> en <ámbito>"` (problemas en X, fallas en Y, demoras en Z, cancelaciones de W). Streaming Phase 2 does **not** propose `add` for this type — complaints accumulate and the **consistency pass** generalizes them into recurring patterns. Sentiment is always negative for complaint assignments — the field can default rather than being asked again.

When a complaint pattern recurs over many items it may *also* sustain a parallel `entity_stance` ("el ayuntamiento es ineficiente"). The two stances coexist as separate entries; the consistency pass can flag the implicit link.

---

#### `gratefulness` — reconocimiento positivo, frecuentemente sobre un evento

Positive mirror of `complaint`. The catalog entry is the **recurring gratitude pattern**.

| Real comment | Stance label (what goes in the catalog) |
|---|---|
| "gracias por arreglar la calle de Hidalgo" | "agradecimiento por mantenimiento vial" |
| "qué padre que pusieron luminarias en el parque" | "agradecimiento por iluminación pública" |
| "gracias al ayuntamiento por el festival, estuvo increíble" | "agradecimiento por eventos culturales" |
| "felicidades a los policías por el operativo" | "reconocimiento a la labor policial" |
| "se nota que están trabajando en la limpieza, gracias" | "agradecimiento por servicios de limpieza" |

**Notes.** Entries phrased as `"agradecimiento por <ámbito>"` or `"reconocimiento a <ámbito>"`. Sentiment is always positive. Same growth model as `complaint`.

---

#### `suggestion` — propuesta de acción dirigida al cliente

The user proposes an action the customer should take. The catalog entry is the **recurring petition pattern**.

| Real comment | Stance label (what goes in the catalog) |
|---|---|
| "deberían poner topes en la avenida Tecnológico" | "petición de medidas de seguridad vial" |
| "falta una clínica en la zona oriente" | "petición de servicios de salud" |
| "podrían ampliar el horario de atención hasta las 7pm" | "petición de horarios extendidos" |
| "tendrían que actualizar el sistema de predial, está obsoleto" | "modernización de sistemas administrativos" |
| "necesitamos más rutas de transporte público los domingos" | "ampliación del transporte público" |

**Notes.** Entries phrased as `"petición de X"` / `"ampliación de Y"` / `"modernización de Z"`. Sentiment is typically neutral or positive. Same growth model as `complaint`.

---

#### `request` — petición específica de atención, información o ayuda personal

The user wants something specific from the customer about *their own* case. Distinct from `suggestion` (general / public) and `question` (info-only).

| Real comment | Stance label |
|---|---|
| "¿podrían revisar mi expediente 1234?" | *(no entry — tag-only)* |
| "necesito ayuda con un trámite, ¿con quién hablo?" | *(no entry)* |
| "soy María, necesito que me llamen al 442…" | *(no entry)* |
| "llevo 3 días esperando respuesta a mi solicitud" | *(no entry)* |

**Notes.** `stance_id` is always `null`. The assignment exists for coverage stats and to surface volume of unhandled personal asks. If the same `request` shape recurs (e.g. "no me responden mis solicitudes") it will likely *also* sustain a `complaint` ("fallas en respuesta a solicitudes") or `entity_stance` ("el ayuntamiento es inaccesible") — the two can coexist as separate assignments.

---

#### `denuncia` — acusación de irregularidad o ilícito

Stronger than `complaint`: alleges wrongdoing. Reputationally and legally heavy.

| Real comment | Stance label (what goes in the catalog) |
|---|---|
| "vi a un policía recibiendo dinero en Bernardo Quintana" | "denuncias de corrupción policial" |
| "el director de obras es cuate del alcalde, todo es transa" | "denuncias de tráfico de influencias" |
| "están construyendo sin permiso en El Refugio, nadie hace nada" | "denuncias de construcciones irregulares" |
| "el contrato de basura está amañado, lo dieron a dedo" | "denuncias de contrataciones irregulares" |

**Notes.** Entries phrased as `"denuncias de <conducta irregular>"`. Sentiment is always negative. Streaming Phase 2 does *not* propose entries here either — denuncias accumulate; consistency pass generalizes.

When a denuncia is severe enough it should also raise an alert independently of catalog growth (see §8.B catalog health metrics).

---

#### `question` — pregunta abierta dirigida al cliente o sobre él

Information-seeking. Each item-level question is tagged with the **FAQ topic** it belongs to — the recurring inquiry pattern that emerges across many items.

| Real comment | Stance label (FAQ topic) |
|---|---|
| "¿hasta cuándo van a tener cerrada la calle Madero?" | "duración de cierres viales" |
| "¿quién es el responsable del programa de salud?" | "responsables institucionales" |
| "¿dónde puedo pagar mi predial este año?" | "pago del predial" |
| "¿alguien sabe si ya reabrieron el módulo del centro?" | "estatus de módulos de atención" |
| "¿qué documentos necesito para el acta de nacimiento?" | "requisitos para acta de nacimiento" |

**Notes.** Catalog entries are phrased as **topic-style labels**, often noun phrases (*"pago del predial"*, *"horarios de atención"*) or short descriptive phrases (*"duración de cierres viales"*, *"responsables institucionales"*). They read like FAQ section titles. Sentiment is typically neutral.

**Why questions get their own catalog (FAQ catalog).** Recurring questions are a first-class downstream signal: comms teams want a direct list of *"things the public keeps asking us about"* so they can publish documentation. Mapping questions onto complaints (*"información poco clara sobre X"*) muddles the action signal — recurring questions trigger a comms response, not an operational fix. The two are different remediations and different teams.

**Layered with complaints when frustration is present.** With multi-stance per item, a comment like *"¿por qué siempre tardan tanto con los trámites?"* yields **two** assignments: one `question` to the FAQ topic *"tiempos de trámites"* AND one `complaint` to *"demoras en atención"*. The FAQ is the primary signal (the question topic recurs); the implicit grievance is the secondary one. Both lie in their natural type's catalog.

**Streaming behavior.** Same growth model as `complaint` / `gratefulness` / `suggestion`: streaming Phase 2 assigns to existing FAQ entries when one fits and `null` when none does. The **consistency pass** (§5) generalizes recurring question patterns into new FAQ entries (typical case) and *additionally* proposes complaint/entity_stance entries when the question pattern reveals deep customer-perception issues (e.g. recurring opacity questions also seed *"el ayuntamiento es opaco con sus procesos"*).

**Aggregate metrics.** "Top unanswered FAQ topics per period" is a useful comms-gap signal — derived directly from the question catalog's per-entry assignment counts.

---

#### `endorsement` — manifestación de lealtad o rechazo generalizado al cliente

Loyalty signal. Sentiment field carries the polarity (positive = support; negative = opposition). Distinct from `gratefulness` (specific action) and `complaint` (specific grievance).

| Real comment | Stance label (catalog) | Sentiment |
|---|---|---|
| "vamos con FeliFer, es el mejor alcalde que hemos tenido" | "apoyo al alcalde" | positive |
| "sigan así ayuntamiento, todo mi respaldo" | "apoyo al gobierno municipal" | positive |
| "hay que sacarlos en la próxima elección" | "rechazo al gobierno actual" | negative |
| "no apoyo a este gobierno ni un día más" | "rechazo al gobierno actual" | negative |
| "FeliFer no me representa" | "rechazo al alcalde" | negative |

**Notes.** Entries phrased as `"apoyo a <X>"` / `"rechazo a <X>"`. The sentiment field is **required and meaningful** for this type — `apoyo` entries collect positive-sentiment assignments, `rechazo` entries collect negative-sentiment assignments. Two opposite entries (`apoyo al alcalde` and `rechazo al alcalde`) typically coexist whenever the public is divided.

---

#### `noise` — todo lo que no aporta señal sobre el cliente

Catch-all for greetings, off-topic, promotional content, calls-to-action toward other users.

| Real comment | Stance label |
|---|---|
| "buenos días a todos" | *(no entry)* |
| "síganme en mi canal de YouTube" | *(no entry — promotional)* |
| "salgamos a marchar mañana en el centro" | *(no entry — call-to-action toward others)* |
| "feliz cumpleaños a Lupita" | *(no entry — off-topic)* |
| "🙏🙏🙏" | *(no entry — only emoji, no content)* |

**Notes.** `stance_id` is always `null`. An item tagged `noise` should not carry any other stance assignments. Tracking `noise` volume is a coverage metric and a comment-feed health check (high noise % suggests bad upstream filtering).

### 2.3 Tie-break rules

Within a **single utterance fragment** (a single stance idea), if more than one type seems to fit, pick the most specific:

`denuncia` > `request` > `complaint` > `suggestion` > `gratefulness` > `endorsement` > `entity_stance` > `question` > `noise`.

*Rationale:* deeper-impact types win over softer ones; substantive types win over noise.

This priority **does not apply** when an item carries multiple distinct ideas (§2.1) — those become multiple assignments. The priority only resolves ambiguity *within one assignment*.

### 2.4 Why claims are not a stance type

A natural question after seeing the nine types: should `claim` be type number ten? The answer is no, for one structural reason — **scope**.

| Axis | Stance types | Claims |
|---|---|---|
| Catalog scope | per-**customer** | per-**(customer, event)** |
| Lifecycle | durable, multi-event | bound to an event's narrative window |
| Aggregation unit | "this customer is/does X" across all events | "in event Y, customer Z is alleged to have done W" |
| Primary key | `(customer_id, stance_id)` | `(customer_id, event_id, cluster_id)` |
| Driving use cases | brand perception, comms strategy, public-mood trends | event updates, false claims / fake news, clarification timing |

Stances aim to be event-*independent* — that's the whole point. The same `entity_stance` (*"el ayuntamiento es ineficiente"*) gets assigned across articles about road maintenance, water service, public works permits, etc. — cross-event reuse is what makes the catalog useful. None of the nine types would be more identifiable if event-bound: each is designed to recur across events.

If we stripped event-scope from claims to fit them into the stance catalog, we'd lose:

- **Event update tracking** — claims are how we follow how a narrative evolves *within* one event ("versión 1: AXA pagó / versión 2: no pagó / versión oficial: pagó parcialmente"). The signal only exists when the cluster is bound to the event.
- **False-claim detection** — fact-checking is intrinsically event-scoped: *"X happened in event Y on date Z"* is verifiable; *"the customer is X"* is a perception.
- **Clarification timing** — *"a high-importance claim is gaining traction in event Y"* alerts trigger on `importance_max ≥ 2 ∧ is_new` within an event's freshness window. Without event scope, these can't be computed.

Conversely, keeping claim-style event-scope while merging them as a stance type would propagate event-scope to all the other types — fragmenting the per-customer catalog into N per-event micro-catalogs and destroying the cross-event consistency that's the point of stances. There's no way to have both.

#### Two parallel pipelines that co-emit per item

The right model is two parallel pipelines that share **per-item co-emission**:

- **Stance pipeline** — per-customer catalogs, customer-anchored. Streaming Phase 2 produces stance assignments. Consistency pass (§5) operates per-customer.
- **Claim pipeline** — per-(customer, event) cluster catalogs, event-anchored. Streaming Phase 2 produces raw claims; Phase 4 clusterer maintains the per-event clusters.

A single comment can yield, in the same Phase 2 LLM call:

- one or more stance assignments (typed — `complaint`, `entity_stance`, `denuncia`, …),
- zero or more claims (each tied to an event, with `verbatim`, `importance`, `affected_entity_ids`).

#### Cross-references between the two pipelines

The pipelines reference each other through `event_id`, but neither catalog merges into the other:

- A `complaint` (or any) stance assignment carries `event_id` as a **filter dimension** (already in §7.2): we know the event the assignment was emitted from, but the entry it points at is event-independent.
- A claim carries `event_id` as its **primary anchor**: the cluster only exists within that event's catalog.
- Downstream cuts can correlate the two: *"for items where `denuncia` was assigned, what claim clusters did the same items contribute to?"* — useful, but answerable only because both pipelines exist independently.

The consistency pass (§5) can take a one-way feed from claims into stance proposals: when many claims of a recurring shape accumulate across events for a customer, that's evidence for a candidate `entity_stance` (*"la aseguradora no honra acuerdos"* anchored in repeated dishonored-agreement claims across events). The reverse — pushing stance assignments into the claim catalog — has no meaning, since stances aren't event-scoped.

#### `denuncia` is the closest neighbor of `claim`

Of the nine stance types, `denuncia` is the one most often confused with `claim`. They both allege wrongdoing, but the distinction is **utterance shape vs factual content**:

- `denuncia` is *"the user is alleging X"* — a tag on the utterance, anchored to a per-customer entry like *"denuncias de corrupción policial"*.
- `claim` is *"X happened"* — a structured factual assertion, anchored to a per-(customer, event) cluster.

High-stakes content typically produces both: a `denuncia` stance assignment with the user's grievance phrasing, AND one or more claims with `importance ≥ 2` carrying the structured allegation about the event. Reports that need to surface "what's the public alleging in event Y" pull from claims; reports that need to surface "what kind of allegations recur about customer Z" pull from the `denuncia` entry-set.

### 2.5 Catalog-mutation eligibility per type

| Type | Has its own entry-set? | Streaming Phase 2 can propose `add`? | Consistency pass can propose `add`? |
|---|---|---|---|
| `entity_stance` | yes | **yes** (current behavior) | yes |
| `complaint` | yes | no | yes |
| `gratefulness` | yes | no | yes |
| `suggestion` | yes | no | yes |
| `denuncia` | yes | no | yes |
| `endorsement` | yes (split by sentiment polarity at the entry level) | no | yes |
| `question` | yes (FAQ catalog — topic-style entries) | no | yes |
| `request` | no | n/a | n/a |
| `noise` | no | n/a | n/a |

Streaming-time growth is reserved for `entity_stance` because that's the type most likely to surface novel-but-durable framings the catalog should capture immediately. The other six stance-bearing types accumulate into recurring patterns over many items and are best generalized in batch by the consistency pass.

## 3. Where the type lives

The type lives on **both** the entry and the assignment, but for different reasons:

- **On the entry (`primary_type`):** entries within each stance-bearing type share a *semantic shape* (see the per-type tables in §2.2). An `entity_stance` entry reads `"<sujeto> es/hace <cualidad>"`. A `complaint` entry reads `"problemas en <ámbito>"` / `"fallas en <Y>"`. A `suggestion` entry reads `"petición de <Z>"`. A `question` entry is a topic-style FAQ label (*"pago del predial"*, *"horarios de atención"*). Forcing all of those into one untyped catalog produces shape salad and breaks the prompts. Each entry declares its `primary_type` and lives in the entry-set for that type.

- **On the assignment (`stance_type`):** the assignment's type matches the type of the entry it points at. For tag-only types (`request`, `noise`) the assignment has `stance_id = null` and the type lives only on the assignment.

Practical consequence: `tags_gpt/catalogs.py:StanceCatalog` keeps its single flat `entries: dict[str, StanceEntry]` (entry ids are globally unique), but every `StanceEntry` declares its `primary_type`. Type-scoped queries become filter operations on the flat dict — `iter_entries(types=…)`, `summary(types=…)`, `snapshot(types=…)`. Seven *logical* buckets at v1 (one per stance-bearing type, including the FAQ-shaped `question` catalog); a single physical dict. The bootstrap pass (`tags_gpt/bootstrap.py:StanceBootstrapStep`) produces only `entity_stance` entries today; the consistency pass (§5) produces entries across all seven stance-bearing types.

**Multi-stance per item.** A single source item can have multiple assignments referencing entries of different types — e.g. one `complaint` assignment to "problemas en cobros" and one `entity_stance` assignment to "el ayuntamiento es ineficiente" emitted from the same comment. The data model already supports this (assignments are a list); the prompt and orchestrator changes in §7 are what enable it in practice.

When the type is non-stance-bearing (`request`, `noise`), the assignment still exists as a row but `stance_id` is `null`. Keeping the row preserves coverage statistics ("how many items did we tag total" vs "…how many got a stance entry").

## 4. Tagging orchestration — two-pass design

The current single-pass Phase 2 (one LLM call per *(article, event)* that does triage + entry assignment + proposals + claim extraction at once) degrades as the catalog grows: every call must hold all entries across all seven stance-bearing types in context, the prompt has to teach all type shapes simultaneously, and any catalog mutation invalidates the whole call's cache. A two-pass split fixes all three.

### 4.1 Phase 2a — Type triage (one call per article batch)

**Input:** customer context, event summary, items (article + comments).
**Output per item:** a list of `{stance_type, brief_summary, sentiment, importance_hint}` — one entry per distinct stance idea the item carries — plus `claims`. The LLM is *not* asked to pick a catalog entry; just to identify *what kinds of stance ideas exist*.

**Catalog-independent.** Phase 2a never loads any stance entry-set. Its cache key —
`(model, customer_id, event_id, items_payload)` — is therefore stable across every catalog mutation. Massive cache hit rate over time.

**Cheap model is appropriate.** Triage is essentially classification + light claim extraction; a fast/cheap model (gemini-2.5-flash-lite, similar to the linker disambiguator) is enough. Each step in `tags_gpt/tagging.py` already takes a `model` kwarg with an env-var default — `TypeTriageStep` adds `OPENROUTER_TYPE_TRIAGE_MODEL`; `StanceTagger` keeps `OPENROUTER_STANCE_TAGGER_MODEL` for the per-type calls.

**Output is a routing manifest.** Each item is annotated with which type-buckets it has content in. Phase 2b only invokes the buckets that actually matter — items with no stance ideas don't trigger any Phase 2b call.

### 4.2 Phase 2b — Per-type enrichment (one call per active type)

For each stance-bearing type that Phase 2a flagged as present in any item, run one Phase 2b call:

**Input:** customer context, event summary, the items that Phase 2a flagged for *this type*, this type's *entry-set only* (not the full combined catalog), this type's growth eligibility (`add` proposals allowed only for `entity_stance` per §2.5).

**Output per (item, type):** `stance_assignments` for this type (item → entry_id, with `null` accepted to flag *"no entry fits — defer to consistency pass"*), plus `stance_proposals` (only `entity_stance` may grow at streaming time; other types emit `null`).

**Per-type prompts use the per-type entry shape directly:**
- `entity_stance` prompt teaches the *"<sujeto> es/hace <cualidad>"* shape with `entity_stance`-specific examples.
- `complaint` prompt teaches *"problemas en X"* / *"fallas en Y"* with `complaint`-specific examples.
- `gratefulness` prompt teaches *"agradecimiento por X"*.
- `suggestion` prompt teaches *"petición de X"* / *"ampliación de Y"*.
- `denuncia` prompt teaches *"denuncias de <conducta irregular>"*.
- `endorsement` prompt teaches *"apoyo a X"* / *"rechazo a X"* with sentiment polarity.
- `question` prompt teaches FAQ topic-style labels (*"pago del predial"*, *"horarios de atención"*, *"requisitos para acta de nacimiento"*).

Same per-type tables in §2.2 are the few-shot examples each prompt embeds.

**Granular cache.** Per-call key: `(model, customer_id, event_id, type, items_subset, type_entry_set_snapshot)`. A change to the complaint catalog never invalidates the entity_stance pass. A change to entity_stance only invalidates entity_stance.

**Parallelizable.** The per-type calls are independent and run in parallel after Phase 2a returns.

### 4.3 Cost and latency

Per article batch:
- **Always:** 1 Phase 2a call (catalog-free, lightweight model).
- **0–6:** Phase 2b calls, one per active stance-bearing type Phase 2a flagged. Typical: 1–3.

Total LLM calls per batch: 2–4 (vs 1 today). **More calls, but smaller prompts each:**
- Phase 2a has no catalog payload at all.
- Each Phase 2b loads only one type's entries (~1/6 of the combined catalog at maturity).

Net token cost vs the current single pass:

| Catalog size (combined entries) | Single-pass cost | Two-pass cost | Winner |
|---|---|---|---|
| < 30 (Stage 1, early) | low | slightly higher (overhead of 2a) | single-pass |
| 30–80 (early production) | medium | similar | even |
| 80–200 (mature) | high (full catalog every call) | medium (per-type slice) | **two-pass** |
| > 200 (very mature) | very high | medium | **two-pass clearly** |

Quality wins are independent of cost — the per-type prompt is always sharper than a combined one.

Latency is comparable when Phase 2b calls run in parallel: ~max(Phase 2a, Phase 2b_max) + a coordinator hop, vs the current single LLM round-trip.

### 4.4 Where claims fit

Two options for the streaming hot path; both viable.

- **Option A (combined with Phase 2a):** Phase 2a emits both the type triage manifest *and* claims. Saves one LLM call. Claim extraction is independent of any catalog so it composes cleanly with the catalog-free 2a call. *Recommended for v1.*
- **Option B (separate Phase 2c):** A dedicated claim extractor, like `tags_gpt/` already does today. Cleaner concern separation; pricier. Upgrade to this only if claim quality demands a separate pass.

### 4.5 Failure handling

A failed Phase 2b call for one type doesn't break the others. The orchestrator:
- records partial results (items get assignments for the types whose 2b succeeded),
- marks the failed type's items with a *deferred* flag,
- on the next batch, includes deferred items in that type's 2b input so the next Phase 2b retries them.

### 4.6 Symmetry with the consistency pass

The consistency pass (§5) operates per-type by construction — it re-evaluates each type's entry-set independently, generalizes recurring patterns into the right type's entry-set, and adjudicates per type. The two-pass streaming design mirrors this symmetry: same per-type prompts (with smaller scope) and same proposal vocabulary. Streaming and consistency become two scopes of the *same* per-type machinery, not two unrelated pipelines.

### 4.7 When the single-pass design still wins

- Catalog is small (< 30 entries combined) and not expected to grow beyond ~50.
- Stage 1 prototyping where simplicity > granularity.
- Tight cost ceiling and fine-grained type-aware reports aren't needed.

The current `tags/` and `tags_gpt/` implementations both ship with the single-pass design today. **Migrating to two-pass is a v2 move**, justified once catalog size or per-type quality complaints surface. The data-model changes in §7 land first; the orchestration migration is independent.

### 4.8 Migration path

The data-model changes in §7 (typed entries via `StanceEntry.primary_type`, `stance_type` on `StanceAssignment`, multi-stance per item) are prerequisites — they ship without touching the orchestration. Once they're in:

1. **Add the strategy flag.** New field `StreamingState.tagging_strategy: Literal["single_pass","two_pass"] = "single_pass"` (`tags_gpt/streaming.py`). Default keeps current behavior — `StanceTagger` runs once with the full catalog snapshot, exactly like today.
2. **Implement Phase 2a.** New `TypeTriageStep` in `tags_gpt/tagging.py` and `type_triage_prompt` in `tags_gpt/prompts.py`. Output: `TypeTriageResult` (§7.9). Catalog-free; routes claims through too (Option A in §4.4). Cheap model via `OPENROUTER_TYPE_TRIAGE_MODEL`.
3. **Make `StanceTagger` per-type.** Add `stance_type` and `triage_hints` keyword arguments to `StanceTagger.tag(...)` (§7.9). When set, the tagger calls `catalog.snapshot(types={stance_type})` instead of the full snapshot, and the prompt is rendered with the per-type entry-shape exemplars from §2.2. Keep the no-arg call signature working (defaults to `stance_type="entity_stance"`).
4. **Wire the two-pass branch in `StreamingTagsPipeline.process_batch`.** When `state.tagging_strategy == "two_pass"`: call `TypeTriageStep.triage` once, then iterate active types and call `StanceTagger.tag` per type in parallel; updaters and `Claim*` steps stay unchanged.
5. **A/B against single-pass.** Run the existing fixtures through both strategies; compare assignment counts per type, proposal quality, snapshot diff, total LLM calls, total tokens. Use `CachedJsonLlm` so re-runs are deterministic.
6. **Roll out per-type prompts incrementally.** Start with `entity_stance` (it already has streaming-time growth, smallest behavior change). When the metrics look good, add per-type prompts for the other six stance-bearing types one at a time; the §2.2 tables make this mostly mechanical.
7. **Flip the default.** Once six+ types ship, change the `tagging_strategy` default to `"two_pass"` in `LocalRunConfig` and `StreamingState`.

Each step ships independently; the whole pipeline never has to migrate at once.

## 5. Consistency pass

Streaming tagging is myopic — each batch sees only its own items and the current catalog. Over hundreds or thousands of items, the catalog drifts: redundant entries that should be merged, missed recurring patterns that should have become entries, entries created early in the run that no longer match how they're being used today.

The **consistency pass** is a periodic global re-evaluation, similar in spirit to the bootstrap pass but run *with the catalog already populated*. It produces bulk mutation proposals (add / merge / rename / retire / re-route) that the adjudicator vets in batch.

### When does it run?

Trigger options (combinable; default = OR):

- **Item counter:** every `N` new processed items (default `N = 200`, configurable per customer).
- **Time interval:** at least every `T` days even if the counter hasn't tripped (default `T = 7`).
- **On-demand:** explicit `customer.run_consistency_pass()` invocation (manual / cron / dashboard button).

State needed to enforce these triggers:

- count of items processed since the last pass,
- timestamp of the last pass,
- threshold knobs (`N`, `T`).

### What does it do?

1. **Sample selection.** Pick a representative slice of items processed since the last pass:
   - stratified by `stance_type` so every type gets reviewed,
   - prioritising items the streaming tagger flagged as worth-keeping (see §6 *Worthiness flag*),
   - including a tail of items that fell into `noise` or that were tagged with low confidence so we audit the bottom of the distribution.
   Default sample size: 200–400 items.

2. **Re-evaluation prompt.** Run a single LLM call (similar to bootstrap) with: the customer context, the *current full catalog* (all seven entry-sets), the sample, plus aggregate stats (per-entry assignment counts, per-type counts, last-used timestamps, recurring unmapped patterns per type). Ask the LLM to propose:
   - **add** — patterns recurring in the sample that aren't represented yet, including:
     - new entry-driving stances directly observable in the sample (the bootstrap-style case),
     - new **FAQ topic entries** for recurring `question` patterns that don't yet have a topic in the question catalog (e.g. 30 items asking variants of *"¿cómo pago el predial?"* → propose a `question` entry *"pago del predial"*),
     - **secondary stance entries** that a question pattern reveals when the recurring inquiry implies a deeper customer-perception issue. These are *additional* to the FAQ entry, not replacements — e.g. when *"¿por qué nunca contestan?"* recurs, also propose a `complaint` entry *"fallas en respuesta a solicitudes"* or an `entity_stance` *"el ayuntamiento es inaccesible"*. With multi-stance per item, future items hitting the pattern can sustain both assignments at once.
   - **rename** — entries whose label has drifted from how they're actually used;
   - **merge** — pairs of entries that are now near-duplicates;
   - **retire** — entries unused in the last `M` items (catalog hygiene);
   - **re-route** — assignments that should point at a different entry (mass correction);
   - **back-route** — given a newly-added entry, retroactively attach assignments to past items that matched the pattern (optional; pricier — defer to v1.5 if the basic pass works).

3. **Adjudication.** Proposals run through `StanceUpdater._decide(...)` (`tags_gpt/tagging.py`) reusing the same prompt and decision space as the streaming-time adjudicator (`stance_update_prompt` in `tags_gpt/prompts.py`). Same `StanceDecision` action enum (`accept` / `reject` / `rename` / `generalise`).

4. **Apply.** Mutations apply directly through `StanceCatalog` methods on `tags_gpt/catalogs.py`: `add_entry` / `rename` / `merge` (existing), plus new `retire(stance_id)` and `reroute(from_id, to_id)` (§7.5). The Updater pattern is preserved — the consistency-pass step (`tags_gpt/consistency.py:ConsistencyPassStep`, §7.10) owns these mutations the same way `StanceUpdater` owns streaming-time mutations.

### Why this is worth the LLM cost

- One periodic global pass costs *less* than running the adjudicator on every borderline streaming proposal — most streaming proposals can be deferred and decided in batch with full context.
- Catalog quality improves over time instead of decaying.
- Provides a clean place to introduce **type-conditional catalog growth** (i.e. allow `complaint` / `gratefulness` / `suggestion` / `denuncia` / `endorsement` to propose entries) without polluting the streaming hot path.

## 6. Worthiness flag (idea, not v1)

At Phase 2 tagging time the LLM could emit a `consistency_relevance: "low" | "medium" | "high"` per assignment (the field already lands on `StanceAssignment` in §7.2), predicting whether the item is a useful exemplar for a future consistency pass. High-relevance items would be:

- novel framings of an existing stance,
- exemplars of an emerging pattern not yet in the catalog,
- borderline cases where the assigned tag wasn't a clean fit.

Low-relevance items would be redundant copies of patterns already well-represented (the 50th item that says exactly the same thing about a known stance).

The consistency pass would prefer high-relevance items for its sample, with a baseline of random selection so we don't over-fit to the LLM's own bias.

**Cost framing:** asking for the flag adds a single short field to the Phase 2 output JSON — minimal token cost, large downstream value if it works. Worth A/B testing once §5 lands.

**Decision deferred:** include in the data model now (so we don't migrate twice) but treat it as optional / unused at v1.

## 7. Data model additions

The canonical implementation is **`src/entities/tags_gpt/`**. Every change below names the exact module + dataclass that gets touched. Where a class already exists in `tags_gpt`, this section says "field added" / "method added" rather than redefining the class.

The `tags_gpt` architecture relevant to this design:

- `models.py` — pure dataclasses (`Customer`, `ContentGraph`, `SourceItem`, `LinkedEvent`, `LinkResult`, `StanceEntry`, `StanceAssignment`, `StanceProposal`, `StanceTagging`, `StanceDecision`, `RawClaim`, `ClaimCluster`, `ClaimAssignment`, `ClaimTagging`, `ClaimDecision`, `ClaimMutation`, `StepSummary`, `EventTagResult`, `ArticleProcessResult`).
- `catalogs.py` — in-memory stores (`EventStore`, `StanceCatalog`, `ClaimCatalog`, `ClaimCatalogStore`).
- `tagging.py` — independent steps (`StanceTagger`, `StanceUpdater`, `ClaimTagger`, `ClaimUpdater`). Each step produces decisions; the *Updater* steps own all catalog mutations (Phase 5 is inline, no separate `apply.py`).
- `streaming.py` — `StreamingTagsPipeline` composes the steps with explicit boundaries; `StreamingState` holds `event_store`, `stance_catalog`, `claim_catalogs`, `items_seen`.
- `prompts.py` — prompt-builder functions (`stance_tagging_prompt`, `stance_update_prompt`, `claim_tagging_prompt`, `claim_update_prompt`, …); no separate `prompts/*.txt` templates.
- `llm.py` — `JsonLlm` Protocol; `OpenRouterJsonLlm`, `CachedJsonLlm`, `ScriptedJsonLlm` implementations. Caching lives in the LLM adapter, not in each tagger.
- `bootstrap.py` — `StanceBootstrapStep`. The consistency pass step (§5) follows the same shape.
- `persistence.py` — `load_content_graph`, `save_snapshot` (snapshot uses each dataclass's `to_dict()` — adding fields auto-propagates).
- `runner.py` — `LocalRunConfig`, `run_local_stream`.

### 7.1 New enums (`tags_gpt/models.py`)

Add next to the existing `SourceKind` Literal alias:

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

Sentiment = Literal["positive", "negative", "neutral"]

# Types that have their own entry-set inside the catalog.
# `question` is included as the FAQ catalog (topic-style entries).
STANCE_BEARING_TYPES: set[StanceType] = {
    "entity_stance",
    "complaint",
    "gratefulness",
    "suggestion",
    "denuncia",
    "endorsement",
    "question",
}

# Types whose entries can grow at streaming time (Phase 2). Currently only
# `entity_stance`; the rest grow only via the consistency pass (§5).
STREAMING_GROWABLE_TYPES: set[StanceType] = {"entity_stance"}

# Types that exist as assignment-only tags (no catalog entry; stance_id=None).
TAG_ONLY_TYPES: set[StanceType] = {"request", "noise"}
```

### 7.2 `StanceAssignment` — new fields (`tags_gpt/models.py`)

The class today holds `source_item_id`, `source_kind`, `customer_id`, `stance_id`, `event_id`, `reason`, `assigned_at`. Add four fields and relax one:

```python
@dataclass
class StanceAssignment:
    source_item_id: str
    source_kind: SourceKind
    customer_id: int
    stance_id: Optional[str]                          # CHANGED: was str → Optional[str] (null for noise/request, or for stance-bearing types when no entry fits yet)
    stance_type: StanceType = "entity_stance"         # NEW — required at semantic level; default for back-compat with existing snapshots
    sentiment: Optional[Sentiment] = None             # NEW — required for endorsement; auto-set for complaint/gratefulness; optional otherwise
    consistency_relevance: Optional[Literal["low","medium","high"]] = None  # NEW — §6 worthiness flag
    consistency_used: bool = False                    # NEW — set to True once a consistency pass has consumed this assignment
    event_id: Optional[str] = None                    # unchanged
    reason: str = ""                                  # unchanged
    assigned_at: str = field(default_factory=now_iso) # unchanged
```

`StanceAssignment.to_dict()` is `dict(self.__dict__)` today — new fields auto-propagate to snapshots without code change. Multi-stance per item is *already* supported because `StanceCatalog.assignments` is a list (§2.1 needs no schema change).

Migration: existing snapshot rows lack `stance_type` → load with `"entity_stance"`; lack the consistency fields → load with defaults.

### 7.3 `StanceEntry` — gains `primary_type` (`tags_gpt/models.py`)

The class today holds `id`, `label`, `description`, `created_at`, `aliases`. Add one field:

```python
@dataclass
class StanceEntry:
    id: str
    label: str
    description: str = ""
    primary_type: StanceType = "entity_stance"        # NEW — see §7.1
    created_at: str = field(default_factory=now_iso)
    aliases: list[str] = field(default_factory=list)
    origin_event_id: Optional[str] = None             # NEW (optional; see §8.G)
```

Note: there is **no `n_assignments` field** in `tags_gpt` — counts are derived on demand by `StanceCatalog.summary()` walking `assignments`. This stays. Type-scoped counts are a `summary(by_type=...)` filter.

`StanceEntry.new(label, description, entry_id=...)` adds an optional `primary_type` parameter (default `"entity_stance"`).

Migration: existing entries default `primary_type = "entity_stance"` on load.

### 7.4 `StanceCatalog` — typed-aware operations (`tags_gpt/catalogs.py`)

`tags_gpt`'s `StanceCatalog` keeps a single flat `entries: dict[str, StanceEntry]`. We **keep that flat layout** — entry ids are globally unique and every entry now declares its `primary_type`, so type-scoped queries are filter operations on the flat dict. This is less invasive than splitting into `entries_by_type` and lets every existing method (`add`, `add_entry`, `assign`, `rename`, `merge`, `delete`, `summary`, `snapshot`, `to_dict`) keep its current signature.

Method changes:

- **`add(label, description="", entry_id=None, primary_type="entity_stance")`** — new keyword arg.
- **`add_entry(entry)`** — unchanged; uses `entry.primary_type` directly.
- **`assign(assignment)`** — add a type-consistency check:
  ```python
  entry = self.entries.get(assignment.stance_id) if assignment.stance_id else None
  if entry and entry.primary_type != assignment.stance_type:
      return False  # type mismatch — drop
  ```
- **`summary(*, event_id=None, top_n=None, types=None)`** — new `types: set[StanceType] | None` arg; when set, only count assignments whose `stance_type ∈ types` (or whose entry's `primary_type ∈ types`).
- **`iter_entries(types=None)`** — new helper returning `Iterable[StanceEntry]` filtered by `primary_type`.
- **`snapshot(types=None)`** — extend to optionally narrow to a subset of types (used by the per-type Phase 2b prompt — see §4.2 — to load only one type's entries).

### 7.5 `StanceCatalog` — new mutation operations (`tags_gpt/catalogs.py`)

The consistency pass needs two operations not in the catalog today. Both touch only `tags_gpt/catalogs.py`:

- **`retire(stance_id) -> bool`** — soft-delete: removes the entry from `entries` and pushes it onto a new `retired_entries: dict[str, StanceEntry]` field. Existing assignments stay tagged with the retired id (history preserved). New tagging can't assign to retired entries because `assign()` checks `entries`, not `retired_entries`.
- **`reroute(from_id, to_id) -> int`** — bulk-rewrite assignments. Distinct from existing `merge(src, dst)`: `reroute` keeps `from_id` alive in `entries` (for cases where the source entry is still valid; we just want to redirect a subset). Returns the count of rewritten assignments.

The existing `merge(src, dst)` already does delete-source + reroute; keep it for the LLM-driven *merge* mutation. `reroute` is additionally callable from outside the LLM (admin UI, post-hoc cleanup).

### 7.6 `Customer` — new state for consistency triggers (`tags_gpt/models.py`)

The dataclass today mirrors `kgdb.entities` columns + joined helpers. Add (and include in `to_dict()` / `from_dict()`):

```python
@dataclass
class Customer:
    # ... existing kgdb fields ...
    # NEW — consistency-pass state (Stage 1 in-memory; Stage 2 lives in kgdb on a sibling table)
    items_processed_total: int = 0
    items_processed_since_last_pass: int = 0
    last_consistency_pass_at: Optional[str] = None    # ISO timestamp
    last_consistency_pass_count: int = 0
    consistency_pass_threshold_items: int = 200
    consistency_pass_threshold_days: int = 7

    def consistency_pass_due(self, now: datetime) -> bool:
        if self.items_processed_since_last_pass >= self.consistency_pass_threshold_items:
            return True
        if self.last_consistency_pass_at is None:
            return False  # never run; only fires once the counter trips
        last = datetime.fromisoformat(self.last_consistency_pass_at)
        return (now - last).days >= self.consistency_pass_threshold_days
```

`StreamingTagsPipeline.process_batch` increments the counters after each article finishes; the pipeline (or its caller) checks `customer.consistency_pass_due(now)` and dispatches to the consistency-pass step (§5).

`Customer.to_dict()` / `from_dict()` need explicit handling for these fields (unlike `StanceAssignment`, `Customer.to_dict` is hand-written rather than `dict(self.__dict__)`).

### 7.7 `StanceTagging` — typed coverage (`tags_gpt/models.py`)

The class today is `assignments: list[StanceAssignment]`, `proposals: list[StanceProposal]`, `dropped_assignments: int`. Add coverage counters:

```python
@dataclass
class StanceTagging:
    assignments: list[StanceAssignment] = field(default_factory=list)
    proposals: list[StanceProposal] = field(default_factory=list)
    dropped_assignments: int = 0
    n_assignments_by_type: dict[StanceType, int] = field(default_factory=dict)   # NEW
    n_items_tagged_no_stance: int = 0                                            # NEW — items whose only assignments were tag-only or none
```

`StanceTagger._parse_response()` populates `n_assignments_by_type` and `n_items_tagged_no_stance` while iterating the LLM output.

The prompt-shape change in `prompts.py:stance_tagging_prompt`:
- Output `assignments` items now require `stance_type` and (optionally) `sentiment` next to `stance_id` and `reason`.
- The instruction explicitly allows *multiple* `assignments` entries per `source_item_id` — multi-stance per item.
- Streaming Phase 2 still only allows `add` proposals for `stance_type == "entity_stance"`. Other types' "I'd add this if I could" hints get emitted as `proposals` with `kind="add"` but the `StanceUpdater` will reject them at `_apply_decision` time when `proposal.kind == "add" and the proposal's implied type ≠ entity_stance`. Or — cleaner — `StanceProposal` gains a `stance_type` field and `StanceUpdater` filters on it.

### 7.7.1 `StanceProposal` — gains `stance_type` (`tags_gpt/models.py`)

```python
@dataclass
class StanceProposal:
    kind: Literal["add", "rename"]
    label: str
    description: str = ""
    stance_type: StanceType = "entity_stance"        # NEW — required field at streaming time only "entity_stance" is accepted
    source_item_ids: list[str] = field(default_factory=list)
    src_stance_id: Optional[str] = None
```

`StanceUpdater._apply_decision` adds:

```python
if proposal.kind == "add" and proposal.stance_type not in STREAMING_GROWABLE_TYPES:
    summary.inc("rejected_streaming_growth_blocked")
    return
```

### 7.8 `ClaimTagging` — already correct (`tags_gpt/models.py`)

`ClaimTagging` already supports multi-claim per item (`claims` is a list) and tracks `dropped_off_customer` / `dropped_invalid`. The `include_comments` flag and `claim_source_kinds()` helper already filter at the input layer. **No changes for the type-system work.** Optional cosmetic addition: a `dropped_from_comments` counter when `include_comments=False`, but the input-layer filter makes the LLM never see comments in that mode, so the counter would always be zero.

### 7.9 New step classes for the two-pass orchestration (§4)

When we adopt the two-pass design, three new artefacts land in `tags_gpt/`:

```python
# tags_gpt/models.py
@dataclass
class TypeTriageItem:
    source_item_id: str
    source_kind: SourceKind
    stance_type: StanceType
    brief_summary: str
    sentiment: Optional[Sentiment] = None
    importance_hint: Optional[Literal["low","medium","high"]] = None

@dataclass
class TypeTriageResult:
    triaged: list[TypeTriageItem] = field(default_factory=list)
    claims: list[RawClaim] = field(default_factory=list)        # Option A: claims travel with Phase 2a
    n_items_seen: int = 0
```

```python
# tags_gpt/tagging.py — new step (Phase 2a)
class TypeTriageStep:
    def __init__(self, customer: Customer, llm: JsonLlm, *, model: Optional[str] = None): ...
    def triage(self, event: LinkedEvent, items: list[SourceItem]) -> TypeTriageResult: ...
```

```python
# tags_gpt/tagging.py — Phase 2b: StanceTagger gains a `stance_type` parameter
class StanceTagger:
    def tag(
        self,
        event: LinkedEvent,
        items: list[SourceItem],
        catalog: StanceCatalog,
        *,
        stance_type: StanceType = "entity_stance",   # NEW — when set, only loads catalog.snapshot(types={stance_type})
        triage_hints: Optional[list[TypeTriageItem]] = None,  # NEW — restrict items to those Phase 2a flagged for this type
    ) -> StanceTagging: ...
```

`StreamingTagsPipeline.process_batch` (in `tags_gpt/streaming.py`) gains a config knob:

```python
@dataclass
class StreamingState:
    # ... existing ...
    tagging_strategy: Literal["single_pass", "two_pass"] = "single_pass"
```

When `two_pass`, the pipeline calls `TypeTriageStep.triage` once per batch, then iterates `STANCE_BEARING_TYPES ∩ {types observed in triage}` calling `StanceTagger.tag(..., stance_type=t, triage_hints=...)` per type. Updaters and Claim* steps stay unchanged (the per-type `StanceTagger` outputs the same `StanceTagging` shape; updaters don't care which type produced it).

### 7.10 `ConsistencyPassStep` (mirror of `StanceBootstrapStep`)

```python
# tags_gpt/consistency.py (new module — keep alongside bootstrap.py)
class ConsistencyPassStep:
    def __init__(
        self,
        customer: Customer,
        llm: JsonLlm,
        stance_updater: StanceUpdater,         # reuses existing adjudication logic
        *,
        model: Optional[str] = None,
        sample_size: int = 300,
    ): ...

    def run(
        self,
        catalog: StanceCatalog,
        sample: list[StanceAssignment],
        items_seen: dict[str, SourceItem],
        claim_catalogs: ClaimCatalogStore,     # input signal — claims feed proposals (one-way)
    ) -> ConsistencyPassResult: ...
```

Sampling, prompt construction, and applying mutations all live in this step. It calls `StanceUpdater._decide(...)` (or its public wrapper) to adjudicate the proposals it generates.

### 7.11 `ConsistencyPassResult` (new dataclass, `tags_gpt/models.py`)

```python
@dataclass
class ConsistencyPassResult:
    customer_id: int
    started_at: str
    finished_at: str
    sample_size: int
    sample_strategy: dict[str, Any]                 # {"stratified_by_type": {...}, "by_relevance": {...}}
    proposals: list[StanceProposal]                 # add / rename — typed across all six non-entity_stance types
    merge_pairs: list[tuple[str, str]]              # (src_id, dst_id) per type
    retire_ids: list[str]                           # stance_ids retired (across types)
    reroute_pairs: list[tuple[str, str]]            # (from_id, to_id) — usually intra-type
    decisions: list[StanceDecision]                 # adjudicator decisions matching `proposals` 1:1
    summary: StepSummary                            # counts (proposed/accepted/rejected/renamed/generalised/retired/rerouted/back_routed)

    def to_dict(self) -> dict[str, Any]: ...
```

Stored alongside the regular snapshot for audit (`persistence.save_snapshot` gains an optional `consistency_pass_result` parameter, or there's a new `save_consistency_pass(...)` sibling).

### 7.12 Persistence — auto-propagation (`tags_gpt/persistence.py`)

`save_snapshot` already serialises via `to_dict()` on `EventStore`, `StanceCatalog`, `ClaimCatalogStore`, all of which call `to_dict()` on their member dataclasses. Most of §7.x rides this for free:

| Field added | How it lands in the snapshot |
|---|---|
| `StanceAssignment.{stance_type, sentiment, consistency_relevance, consistency_used}` | auto-propagates via `dict(self.__dict__)` |
| `StanceEntry.{primary_type, origin_event_id}` | needs `to_dict` update — `StanceEntry.to_dict` is hand-written |
| `StanceProposal.stance_type` | auto-propagates (proposals aren't snapshotted today, but if added) |
| `Customer.{items_processed_*, last_consistency_*, consistency_*}` | needs `to_dict` / `from_dict` updates — both are hand-written |
| `StanceTagging.{n_assignments_by_type, n_items_tagged_no_stance}` | derived counters; not snapshotted directly (they live on `EventTagResult`) |

Migration on load: missing fields default per dataclass defaults — back-compat with snapshots written before this change.

## 8. Other ideas worth exploring

The user-facing question is *"what other ideas come from this direction?"*. Listing without committing — these are future-work seeds, ranked by how naturally they compose with §2-§7.

**A. Per-assignment confidence.**
Phase 2 emits a `confidence: 0..1` (or low/med/high) per stance_assignment. Low-confidence assignments are review candidates for the consistency pass. Combines with §6: confidence and worthiness are different — an assignment can be high-confidence and high-worthiness (canonical example) or low-confidence and high-worthiness (interesting edge case the catalog doesn't cover well).

**B. Catalog health metrics.**
Derived per pass:
- *orphan entries* — no assignments in the last `M` items,
- *near-duplicate entries* — embedding similarity > τ,
- *over-loaded entries* — > X% of all assignments concentrate here (probably under-fragmented),
- *under-used types* — types with < Y assignments overall (signal: prompt isn't surfacing them).
Surface these to the dashboard and as input to the consistency pass.

**C. Type-conditional prompts.**
Already covered by §4 (the two-pass orchestration). The C entry stays here as a reminder that even a v2-tier system might further specialise a single type's prompt (e.g. dedicated `denuncia` extractor with extra adversarial guards) without rewriting the rest.

**D. Cross-customer catalog seeding.**
A new customer in the same sector (two municipalities, two insurers) likely shares many entity_stance entries. Cache "sector templates" derived from existing customer catalogs — bootstrap a new customer from `customer.sector` instead of a cold start.

**E. Sentiment as its own field.**
Already implied by §7.2 — `sentiment` is on the assignment, not baked into the type. Lets `endorsement` carry positive/negative polarity without splitting the type, and lets `complaint`/`gratefulness` reinforce their implicit polarity (or break with it: a sarcastic *"gracias por nada"* is `gratefulness` × negative).

**F. Type co-occurrence per source.**
Even with disjoint types per item, the *event* a customer is tagged into can be characterized by its type mix ("this event is 70% complaint, 20% question, 10% noise"). Cheap aggregate; doesn't need extra LLM work.

**G. Entry origin tracking.**
Add `origin_event_id`, `origin_consistency_pass_id` on `StanceEntry`. Lets the consistency pass identify entries that were created during one specific event (and may have over-fit to it) and re-evaluate them later.

**H. Adjudicator + consistency pass deduplication.**
If the same proposal recurs across passes (the LLM keeps proposing the same `add` and the adjudicator keeps `reject`-ing it), record the rejection so the next pass can skip it without re-spending LLM tokens. A small `rejected_proposals_history` on the catalog.

**I. Item-level "skip" signal at retrieval.**
Items that the streaming tagger marks `noise` could short-circuit the rest of the pipeline (no claim extraction either). Adds a per-item gating step but saves Phase 4 calls when an article comment is just a greeting.

**J. Adversarial / drift QA.**
Periodically re-tag a handful of items blind (no current-catalog context shown) and compare with production tags. Diverging tags signal drift.

**K. User-driven curation hooks.**
Persisting decisions made by humans (the eventual *"I disagree, this should be a different stance"* feedback) into the catalog as adjudicator-equivalent decisions. Out of scope until a UI exists; but design `StanceUpdater` / `ClaimUpdater` so a `source: "human" | "llm"` field on each `StanceDecision` / `ClaimDecision` can be plumbed when ready (and so `StepSummary.notes` records who initiated each mutation).

**L. Time-decayed assignment counts.**
`StanceEntry.n_assignments` is currently monotonic. A decayed variant (`exponentially weighted by recency`) makes orphan detection / retirement more responsive.

**M. Per-type catalogs (only if §5 metrics show divergence).**
If complaints and entity_stances genuinely don't share catalog entries in practice, split into `StanceCatalog` per type. Heavier; only do this when measured.

**N. Streaming "shadow" tagger.**
A cheaper second LLM tags every item in parallel; compare with the production tagger; flag disagreements as consistency-pass candidates. Free worthiness signal.

**O. Sample selection: stratify by event, not just type.**
Long-tail events with few assignments shouldn't be over-sampled by sheer count; one item per event gets a baseline slot.

## 9. Open questions for v1

1. **Phase-2 streaming behavior for non-stance types.** Should tagging emit assignments for `noise` / `question` / `request` items at all, or just count them for coverage and move on? Emitting keeps the per-item record symmetrical; skipping reduces LLM output size. Recommend emit.
2. **Sentiment field source.** Is `sentiment` set by the same Phase 2 LLM call or by a separate (cheaper) sentiment classifier? Recommend same call — token cost is one extra short field.
3. **Consistency pass sample upper bound.** Hard cap at ~400 items? Beyond that the LLM context becomes the bottleneck. Confirm before designing the prompt.
4. **Retired entries — reachable how?** Hidden from streaming tagger; visible to adjudicator and consistency pass. Should retired entries' assignments stay tagged with the retired id, or be re-routed to a successor? Recommend: stay tagged (history preserved), but a future re-tagging job can reroute on demand.
5. **Worthiness flag — Phase 2 or out-of-band?** §6 puts it in Phase 2 (likely Phase 2a in the two-pass design). Alternative: a dedicated cheap classifier per item. Same trade-off as #2.
6. **Migration of existing snapshots.** Existing JSON snapshots have no `stance_type` on assignments and no `primary_type` on entries. Default both to `"entity_stance"` on load — but flag a metric so we know how much of the historical assignment volume is back-filled vs explicitly typed.
7. **Multi-stance upper bound per item.** Comments can in principle mix many ideas. Cap at e.g. 4 assignments per source item to bound output size and force the LLM to consolidate near-duplicates (§2.1)? Recommend yes, soft cap = 4.
8. **Same-type duplicates within an item.** If an item carries two complaints about different topics ("me cobraron mal y nadie contesta el teléfono"), emit two `complaint` assignments to two different entries vs one assignment per item per type? Recommend two assignments — distinct ideas, distinct entries.
9. **`question` has its own FAQ catalog (resolved).** Earlier drafts treated `question` as tag-only and routed recurring patterns into `complaint`/`entity_stance` entries. v1 instead gives `question` its own per-customer entry-set with topic-style labels (*"pago del predial"*, *"horarios de atención"*) — the FAQ catalog. Recurring question patterns become `question` entries directly (primary path); when they also reveal a deeper perception issue, the consistency pass *additionally* proposes a `complaint`/`entity_stance` entry, and multi-stance per item lets future items sustain both. See §2.2 *question* and §5 step 2.
10. **Sentiment for `complaint` / `gratefulness`.** Polarity is structurally fixed (complaint = negative, gratefulness = positive). Is the `sentiment` field still required for these, or auto-set? Recommend auto-set, save the LLM token.

## Pointers

- Design intent for stances: [`tags_overview.md`](tags_overview.md)
- Architecture / class spec: [`tags_impl_plan.md`](tags_impl_plan.md)
- Adjudicator prompt that the consistency pass will reuse: [`prompts/adjudicator.txt`](prompts/adjudicator.txt)
- Bootstrap prompt that the consistency pass mirrors: [`prompts/bootstrap.txt`](prompts/bootstrap.txt)
- KG database schema (Stage-2 target for the consistency-pass state): [`../../../../media-backend-paid/docs/DATABASE_POSTGRES.md`](../../../../media-backend-paid/docs/DATABASE_POSTGRES.md)
