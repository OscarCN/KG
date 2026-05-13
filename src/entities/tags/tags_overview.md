# Tags — Overview & Plan

> **Status: planning, iterating.** This document captures the design intent for the tagging subsystem (stances + claims). It is not a finished spec — expect it to evolve. Implementation will start in this repo under `src/entities/tags/`; longer term the functionality will be moved to a dedicated repo.

## What this is

For a given **customer entity** (the "main entity" — e.g. the Government of Querétaro, an insurance company), extract two complementary kinds of tags from social-media comments, posts, and articles:

- **Stances** — durable attitudes / narratives directed at the customer (e.g. *"the government is inefficient"*, *"AXA refuses to honour its commitments"*).
- **Claims** — specific factual assertions made by users or articles that affect the customer (e.g. *"AXA dishonored the agreement with Hospitales Ángeles"*).

Both come out of the same tagging pass over a corpus, but they live different lives downstream. Stances capture *perceptions*, claims capture *alleged facts*; together they answer "what does the public think about our customer, and what do they say happened?".

## Two tag types

**Both tag types are extracted from the same set of source items: comments, posts, and articles.** Every batch carries a mix of all three, and any single source item can yield zero or one stance plus zero or more claims. The source kind (`article` / `user_post` / `user_comment`) is recorded on every assignment so downstream reports can split / weight by it; the tag definitions themselves don't depend on which kind the source happens to be.

| Dimension | **Stance** | **Claim** |
|---|---|---|
| What it captures | Durable attitude / quality / behaviour attributed to the customer | Specific factual assertion made about an event involving the customer |
| Source items | Articles, posts, and comments | Articles, posts, and comments |
| Scope | **Customer-level** — same label across every event/theme | **Event-level** — heavily event-bound |
| Catalog | **Stance catalog** — per customer, closed, ~tens of entries. Each entry is a single canonical stance label. | **Claim catalog** — per event, open-ended, potentially many entries. Each entry is a **claim cluster**: a canonical phrasing plus the raw claims folded into it. |
| Cardinality | Small | Potentially large (many distinct claim clusters per high-traffic event) |
| Phrasing | Enduring quality (*"X is dishonest"*) | Alleged fact (*"X did Y on Z"*) |
| Verifiable? | No (perception) | Yes in principle (factual; could be cross-checked against authoritative sources later) |
| Retention rule | Always (any tagged source item) | Only when the claim affects the customer |
| Catalog gate | LLM adjudicator approves / rejects / renames additions | LLM clusterer assigns each new raw claim to an existing cluster, creates a new cluster, or drops it (lighter gate — no hard accept/reject vote on entries) |
| Source attribution | Aggregated counts per stance, with `source_kind` retained on each assignment | Per-claim source ids + `source_kind`; cluster aggregates can be split by kind |
| Relationship | **Supported by** claims | **Evidence for** stances |

The two tag types are **complementary, not competing**. The same source item can carry zero or one stance from the customer catalog AND zero or more claims. The synergy is what makes both worth extracting: a body of claims becomes the factual backing for a stance distribution. Instead of just reporting *"30% of items express AXA refuses to honour its commitments"*, we can show *"…and the recurring claims behind that stance are: AXA dishonored the agreement with Hospitales Ángeles, AXA delayed payouts on Flex Plus, …"*.

## Anatomy of a customer's content graph

For a given customer, the relevant content orbits around:

- **the customer itself** (the main entity — `kgdb.entities` row),
- **related entities** that are politically / commercially / topically tied to the customer (officials, subsidiaries, competitors, regulators),
- **events** the customer is involved in or affected by (announcements, accidents, scandals, wins),
- **themes** the customer is associated with (security, mobility, public infrastructure, …).

**The tagging pipeline does not curate this graph itself.** Relevance is decided **upstream**, via the linker's entity-resolution and retrieval (whichever comments / posts / articles the linker has tied to the customer or its related entities are already considered customer-relevant). The tagging pipeline trusts the upstream filter — it consumes a corpus that is, by construction, about the customer's graph. Stances and claims then use that corpus differently: stances pull from the whole corpus to learn enduring qualities of the customer; claims live inside specific events and only matter when they affect the customer.

## Guiding principle — customer-anchored stances

Everything is rooted in the **customer entity**. A run is parameterised by a single customer; the customer drives both content **relevance** ("is this comment about something connected to our customer?") and stance **direction** ("what is the user expressing toward our customer?").

**Scope the catalog at the customer level, not at the event level.** If catalogs were per-(customer, event), each event would grow its own micro-vocabulary of stances and we could never roll them up into higher-level trends or themes for the customer. By keeping the catalog tied only to the customer, the same stance label applies across every event/theme the customer is mentioned in, and time-series / topical aggregations become natural.

**Direct stances at the customer, not at the event.** Phrase stances as enduring qualities or behaviours of the customer, not as event-specific complaints. Events are the *occasion* on which a stance is voiced, not its content.

| Event-scoped stance (avoid) | Customer-anchored stance (prefer) |
|---|---|
| "The government takes too long to fix the roads" | "The government is inefficient / slow to act" |
| "AXA denied my hospital claim" | "AXA refuses to honour its commitments" |
| "The mayor lied about the new metro line" | "The mayor is dishonest / untrustworthy" |

The customer-anchored phrasing holds across many events/themes — when a user filters comments by "road issues", the same `government is inefficient` stance still applies, alongside its counterparts in other event types. That cross-event coherence is what makes the catalog useful for trend analysis.

**Events / themes / related entities are filters, not scopes.** Reports and dashboards slice the same stance distribution by event, theme, time window, geography, source network, etc. A stance like *"government is inefficient"* may peak around road-works coverage in March and around water-cuts coverage in July; the catalog entry stays the same, the lens changes.

## Guiding principle — claims that affect the customer

Claims are extracted as **structured records**, not free text, so they can be deduped, clustered, counted, attributed to sources, and (eventually) verified.

**Extraction shape.** Kept deliberately minimal — the structured artefact is the cluster's canonical phrasing (built in Phase 4), not the individual raw claim. Each raw claim carries:

| Field | Meaning |
|---|---|
| `event_id` | The linked event the claim is anchored to (claims are almost always inside an event's narrative). |
| `affected_entities` | Set of `kgdb.entities` ids the claim is alleged to affect — **must include the customer**, otherwise the claim is dropped. |
| `verbatim` | The original phrasing as it appeared in the source (one representative quote) — the evidence/audit anchor. |
| `source_ids` | Comments / posts / articles asserting the claim. |
| `source_kind` | `article` / `user_post` / `user_comment` — article-asserted vs user-asserted carry different evidentiary weight, surfaced in reports. |
| `importance` | LLM's estimate of how important this claim is for the customer to be aware of, given the event context. Scalar `1` (low) – `3` (high). See [Claim importance](#claim-importance) below. |
| `importance_reason` | One short sentence justifying the importance score (so a human can sanity-check sorting / alerts). |

We deliberately do **not** ask the LLM for `subject` / `predicate` / `object` triples or a separate `time` field. Triple decomposition is brittle for natural-language claims (*"AXA's response was inadequate"* doesn't split cleanly) and the clusterer in Phase 4 doesn't need it — semantic similarity over `verbatim` plus the cluster's canonical phrasing handles equivalence detection. A claim's time, when relevant, is usually inferable from the event itself; if a structured-query need emerges later, we can add a normalisation pass on top of the existing fields.

**Retention rule.** Keep a claim only when it affects the customer (i.e. `customer_id ∈ affected_entities`). The corpus is full of factual chatter that doesn't touch our customer — drop it. Example: *"AXA dishonored the agreement with hospitals"* is retained when the customer is AXA; the same article also containing *"the Tigres won the cup"* drops that second claim (a tangential sponsor mention isn't enough). The same claim re-anchored against a different customer (e.g. Hospitales Ángeles) is a separate run that retains it for that customer.

**Per-event claim catalog, made of clusters.** Claims live in a **claim catalog scoped to a single event** — the structural counterpart to the per-customer stance catalog. The differences are scope (per event vs per customer), openness (open-ended vs closed), and gate policy (clusterer vs adjudicator). Each catalog entry is a **claim cluster**: a canonical phrasing of one allegation, plus the raw claims folded into it (*"AXA dishonored the agreement"* / *"AXA broke the deal with the hospitals"* / *"AXA went back on its word with Ángeles"* → one cluster). Member raw claims keep their `source_ids` so the cluster can cite its evidence; the cluster representative is the most-asserted variant or an LLM-picked summary.

The clusterer's job is lighter than the stance adjudicator's — it doesn't accept/reject claims as a vote, it just decides whether each new raw claim joins an existing cluster, spawns a new one, or drops out. The catalog grows organically with the event's narrative.

**Article-asserted vs user-asserted.** Articles tend to be more factual and less hyperbolic than user comments / posts. Claim assignments share one row layout regardless of source — they are distinguished by `source_kind` (`article` / `user_post` / `user_comment`), the same way `userdb.entities_documents_sentiments_org` already discriminates posts and comments via `doc_index`. Downstream processing and reporting weight or split the two later as needed; the storage layer doesn't pre-commit to a policy.

### Claim importance

The Phase 2 LLM already has the customer context and the event context loaded — for almost no extra cost it can also estimate **how important the claim is for the customer to be aware of**. We use this purely as a flagging / sorting signal, not as ground truth.

- **Scale:** `1` (low) / `2` (medium) / `3` (high). Coarse on purpose — a finer scale invites false precision the LLM can't actually deliver across batches.
- **Anchored to the customer's perspective.** A claim is "high" when it materially affects the customer's reputation, operations, legal exposure, or stakeholder relationships in this event's context — not when it's merely sensational in general.
- **Estimate, not verdict.** Always paired with `importance_reason` (one sentence) so a human can audit and override.
- **What it powers:**
  - **Alerts** — surface high-importance claims as they land, instead of waiting for the daily report.
  - **Sorting** — order claim clusters in dashboards by importance × frequency.
  - **Prioritisation** — when triaging which claims need a human follow-up or fact-check.
- **Cluster aggregation.** When raw claims are folded into a cluster (Phase 4), the cluster carries:
  - `importance_max` — highest member score (drives alerts),
  - `importance_typical` — median or mode across members (drives steady-state sorting),
  - `importance_n_high` — count of `3`-scored members (signal of a claim getting widely amplified at high stakes).
- **Calibration drift.** Importance is comparable *within* a customer × event but not necessarily across them — a `3` for a small local event is not a `3` for a national-level scandal. Treat cross-event aggregates with caution; never compare raw scores across customers.

**Cross-event claim graph (later).** Some allegations naturally span events (*"AXA has delayed payouts in 5 different states"*). For v1, claims stay event-scoped and we accept the duplication. A later pass can link clusters across events when the same allegation pattern recurs (likely via embedding similarity over canonical phrasings, since we don't carry parsed subject/predicate triples).

### New-claim flagging

We don't try to verify claims as true / false (that's an open-ended fact-checking problem). Instead, the pipeline flags **new** claims — allegations that haven't been seen before — so the customer is alerted to *emerging* narratives, not just recurring ones.

- **Per-event novelty** (v1): when Phase 4 creates a brand-new cluster in an event's claim catalog, that cluster is marked `is_new = true` for some configurable freshness window (e.g. its first 24 h of life). New-cluster creations are the natural alerting surface — they map directly to "this event just started carrying an allegation we hadn't seen in this event before".
- **Cross-event novelty** (later): once cross-event linking lands (Q11), a claim cluster can be marked new globally (first time the customer has been hit with this allegation across any event) vs new locally (first time within this event but a known allegation elsewhere). Different alerting thresholds apply.
- **Pairs with importance.** A claim that is both `is_new` and `importance ≥ 2` is the natural "alert this to the customer now" surface. New-but-low-importance is a watch-list signal; old-but-high-importance is a steady-state report signal.
- **Suppress noise.** A cluster spawned in Phase 4 from a single source with no follow-up echoes within the freshness window can be downgraded — being new-but-isolated is often noise, not signal. Decide policy alongside cluster aggregates.

This is much cheaper than verification and gives the customer the "things you should know about that you didn't know yesterday" cut.

## Background — prior art

A working proof-of-concept lives at [`/Users/oscarcuellar/ocn/media/rrss_pg/`](../../../../../rrss_pg/), with the pipeline documented in [`rrss_pg/readme_stances_pipeline.md`](../../../../../rrss_pg/readme_stances_pipeline.md). That implementation:

- Fixes the event/entity per run via a config block (`event_description`, `entity_name`, `entity_description`, `filter_condition`, `platform`).
- Builds the stance catalog in two passes: post-level extraction (Step 3) and comment-level catalog extraction (Step 4 — likes-prioritised, deduped).
- Tags individual comments with stances + sentiment toward the entity in Step 5.
- Aggregates everything into reports and timelines in Step 6.
- Does **not** extract claims — that's new in this design.

The plan here is to **extend that PoC into a production platform** rooted in the KG's linked entities/events, persisted into the existing Postgres schema, and adding claims as a second tag type.

## Core concepts

| Concept | Meaning |
|---|---|
| **Customer (main entity)** | The entity the run is anchored to (e.g. Government of Querétaro, an insurance company). Drives both content relevance and tag direction. Backed by a `kgdb.entities` row. |
| **Customer's content graph** | The customer plus its related entities, events, and themes — the orbit of content that's relevant for tagging. |
| **Stance catalog** | The closed, evolving set of stance entries valid for a given **customer**. Stored as a field on the customer entity record. Gated by the stance adjudicator LLM. |
| **Stance entry** | One canonical stance label inside the stance catalog (e.g. *"the government is inefficient"*). Tagged source items (comments / posts / articles) point at a single entry. |
| **Stance assignment** | A `(source item, customer) → stance entry` link. The source item is a comment, post, or article (with `source_kind` recorded on the assignment). Currently one stance per source item; multi-stance is a future extension. The associated event/theme/entity is recorded as a **lens / filter dimension**, not as part of the stance scope. |
| **Stance catalog mutation** | An addition, rename, or merge proposed by the tagging LLM and approved by the adjudicator LLM. Renames apply retroactively to all source items tagged with the old entry, across every event the customer appears in. |
| **Claim** | A structured record of a specific factual assertion that affects the customer. Extracted per source item (comment / post / article); always anchored to an event. |
| **Claim catalog** | The open-ended set of claim entries valid for a given `(customer, event)`. Structural counterpart to the stance catalog, scoped per event instead of per customer. Composed of claim clusters. |
| **Claim cluster (= claim entry)** | One canonical claim statement inside the claim catalog (e.g. *"AXA dishonored the agreement with Hospitales Ángeles"*) plus the raw claims folded into it. Carries a representative phrasing, the union of member `source_ids`, roll-up `importance_max` / `importance_typical` / `importance_n_high` aggregates, and an `is_new` flag (true while the cluster is within its freshness window after creation — see [New-claim flagging](#new-claim-flagging)). The "cluster" framing emphasises that each entry holds many equivalent raw claims; the "entry" framing emphasises the parallel with stance entries. |
| **Claim assignment** | A `(source item, claim cluster)` link, with the cluster scoped to a specific `event_id`. The source item is a comment, post, or article (with `source_kind` recorded on the assignment). Multi-claim per source item is supported from v1 (a single source item can assert several distinct facts). |
| **Claim catalog mutation** | A new cluster created, an existing cluster's representative renamed, or two clusters merged. Driven by the clusterer LLM rather than a separate adjudicator. Renames apply retroactively to all member assignments within the event. |

**Events are entities.** Linked events live as rows in the `kgdb.entities` table (see [the linker docs](../linking/readme_linking.md#kg-database-persistence)) — there is no separate "events" table. The customer, its related entities, and the events/themes touched by it all share that table; what distinguishes them is `entity_kind` and the tagging pipeline's role assignment (one customer per run, everything else is graph context, with events specifically serving as anchors for claim clusters).

## Pipeline

Inputs to every phase carry the **customer entity** (name + description + role/sector context) plus a description of the customer's content graph (which related entities / events / themes are in scope). The LLMs use this to phrase stances as enduring qualities of the customer and to keep only those claims that affect the customer.

### Phase 1 — Bootstrap stance catalog

**Given:** the customer entity (name + description + content-graph context) + a broad corpus of relevant articles, posts, and comments spanning multiple events / themes the customer touches.
**LLM:** a fairly powerful one (catalog quality matters most here).
**Output:** an initial candidate **stance catalog** grounded in the corpus and phrased as enduring qualities/behaviours **directed at the customer**.

This is the bootstrap step for a customer with no catalog yet. Because the corpus deliberately spans multiple events/themes, the resulting stances generalise across them. The PoC's Step 3 + Step 4 are the reference design (with the scope shifted from a single event to the customer's content graph).

Claims are **not** extracted here — Phase 1 is purely about bootstrapping the stance catalog. Claim extraction happens per batch in Phase 2.

### Phase 2 — Tag content (stances + claims)

**Given:** the customer entity context, the current stance catalog, and a batch of comments/posts/articles (each with the event/theme/related-entity it was retrieved under, used as anchor metadata).
**LLM:** asked to do three things in one pass:

1. **Tag stances.** Assign one stance from the catalog to each item (later: zero or more), expressed about the customer.
2. **Propose stance catalog mutations.** New stances to add, existing ones to rename (broaden / narrow). Proposals must be phrased as customer-anchored qualities, not as event-specific complaints.
3. **Extract claims.** For each item, extract zero or more **structured claims** following the shape in [Guiding principle — claims that affect the customer](#guiding-principle--claims-that-affect-the-customer). Drop claims that don't affect the customer. Every retained claim is anchored to its `event_id` and tagged with an `importance` score (1–3) plus a one-sentence `importance_reason` — see [Claim importance](#claim-importance).

The two tag types come out of the same LLM call to avoid re-reading the same content twice and to let the model use the same context for both.

### Phase 3 — Adjudicate stance catalog mutations

When Phase 2 returns proposed stance mutations, a **separate adjudicator LLM** decides whether to accept each one. It receives:

- the customer entity context,
- the current catalog,
- the proposed change,
- a sample of source items — comments, posts, and articles (including the items that triggered the proposal).

The adjudicator may:
- accept the addition,
- reject it,
- transform an addition into a **rename** of an existing stance,
- generalise the proposed addition (fold an event-specific phrasing into an existing customer-level stance).

### Phase 4 — Update claim catalog (per event)

After Phase 2 (or periodically), run a **claim clusterer LLM** scoped to a single `event_id` to fold raw claims into the event's claim catalog. It receives:

- the customer entity context,
- the event description,
- the existing claim catalog for the event (each cluster represented by its canonical phrasing + a few sample verbatims),
- the new raw claims emitted by Phase 2 against this event.

For each new raw claim it returns one of:
- **assign to existing cluster** (with cluster id),
- **create new cluster** in the catalog (and provide a representative phrasing),
- **drop** (claim is too vague, off-customer, or duplicative noise).

When a cluster's membership grows enough that its canonical phrasing no longer fits well, the clusterer may also propose a **rename** of the cluster representative — handled the same way as stance renames (apply retroactively, member claims keep their source ids). Two clusters can also be **merged** when they turn out to express the same allegation.

### Phase 5 — Apply

- **Stance catalog (per customer)**: accepted additions land in the catalog; accepted renames update the entry **and** rewrite every existing assignment carrying the old label, across every event/theme the customer appears in.
- **Claim catalog (per event)**: new clusters land in the event's catalog; new raw claims assigned to existing clusters; renamed cluster representatives propagate to all member assignments within that event; merged clusters consolidate their member assignments.

The stance catalog is a field on the **customer entity** record; claim catalogs are scoped per event (and indirectly per customer, since only customer-affecting claims are retained).

## Storage model

### Stage 1 — in-memory only

For testing different approaches, everything stays in RAM:

- **Stance catalog** kept per **customer entity** in a Python dict / dataclass.
- **Claim catalog** kept per `(customer, event_id)`, with each entry being a claim cluster (canonical phrasing + member raw claims + importance aggregates).
- **Tagged content** held alongside, each source item (comment / post / article) carrying:
  - the customer id,
  - `source_kind`,
  - the event/theme/related-entity it was retrieved under (filter metadata),
  - assigned stance (one for now, list-shaped for forward compatibility),
  - assigned claim cluster ids (zero or more — multi-claim per source item is allowed).
- A thin **retrieval class** abstracts data fetching. All retrieval methods return mixed source items (articles, posts, comments) — the caller doesn't need to fan out across kinds. At minimum:
  - `get_customer_corpus(customer_id, ...)` — articles, posts, and comments across the customer's content graph (used when bootstrapping the stance catalog).
  - `get_event_items(event_id)` — every source item attached to a linked event the customer is involved in (used for both event-scoped tagging passes and claim clustering).
  - `get_post_comments(post_id)` — comments under a specific post (used when tagging at post granularity; the only method that's intentionally single-kind, since post-level comment threads are a real unit).

This lets us iterate on prompts, batch sizes, customer-graph definitions, adjudication policy, and clustering policy without committing to a schema yet.

### Stage 2 — Postgres persistence (future)

**Stances.** Tagged stances will live on the per-(entity, document) sentiment record. The natural anchor is `entity_id = <customer_id>` on `userdb.entities_documents_sentiments_org` (see [`media-backend-paid/docs/DATABASE_POSTGRES.md`](../../../../../media-backend-paid/docs/DATABASE_POSTGRES.md)) — already org-scoped, already keyed on `(entity_id, doc_id, org_id)`, already carries `sentiment` + `sentiment_reason`. Either:

- **Modify** that table to carry stance data (new `stance` / `stance_reason` columns, possibly arrays once multi-stance is supported), or
- **Replicate** the row layout into a sibling stances table.

Either way, the row's `entity_id` is the **customer**, and the event/theme/related-entity context that surfaced the comment is recorded as auxiliary metadata (filter dimension), not as a second-class anchor.

**Claims.** Claims do not fit naturally into the per-(entity, document) sentiment row because:
- a single source item can assert multiple distinct claims (so it's many-to-one with the doc),
- claims are anchored to an event, not just a document,
- claim clusters need their own canonical record independent of any single source.

Most likely shape (Stage 2 decision):

- A `claim_clusters` table — one row per cluster, keyed on `(customer_id, event_id, cluster_id)`, carrying canonical phrasing, `is_new` flag, importance roll-ups, and member counts.
- A `claim_assignments` table — many-to-many between source documents (`doc_id` + /or `comment_id`) and `cluster_id`, plus the verbatim quote, source kind, and assignment timestamp.

**Where the catalogs themselves live.** The stance catalog is a per-customer field — likely on `kgdb.entities.metadata` (where the linker already writes the validated record) or a dedicated column. The claim catalog for a given `(customer, event)` is the set of `claim_clusters` rows with that `(customer_id, event_id)` — there's no separate "claim catalog" row needed because the cluster table is the catalog.

Decisions deferred to Stage 2 — too many details still need to be covered. Stage 1 design should not lock us out of any path.

## Open questions

| # | Question | Notes |
|---|---|---|
| 1 | **Catalog scope = customer entity** (decided, stances) | Stances are anchored to the customer; events/themes/related entities are filter dimensions. See [Guiding principle](#guiding-principle--customer-anchored-stances). |
| 2 | **Claims are event-scoped, free-form, customer-filtered** (decided) | See [Guiding principle](#guiding-principle--claims-that-affect-the-customer). Open: tuning the clustering aggressiveness. |
| 3 | **Defining the customer's content graph** (decided) | The tagging pipeline does **not** curate the graph. Relevance is decided upstream by the linker / entity-resolution step — whatever content the linker has tied to the customer or its related entities is, by construction, customer-relevant. The tagging pipeline consumes a pre-filtered corpus. See [Anatomy of a customer's content graph](#anatomy-of-a-customers-content-graph). |
| 4 | **Stances toward related entities (later)** | Today only the customer carries stances. A future extension may extract stances toward related entities (e.g. a competing insurer, a regulator) using the same pipeline pointed at a different anchor. Out of scope for v1. |
| 5 | **Are there cases where event-specific stances are still useful?** | A customer-anchored catalog may miss stances that are inherently event-bound (e.g. "the response to the earthquake was disorganised" doesn't generalise to `government is disorganised` cleanly). If we keep finding such cases, an event-overlay catalog may be needed. Track in metrics; revisit. Note: event-bound *facts* are exactly what claims capture, so this question is partly resolved by adding claims. |
| 6 | **Themes as customer-slot anchors?** | Themes are degenerate single entities (one row per `(theme_class, location_up_to_level_3)` — see [`linking/readme_linking.md`](../linking/readme_linking.md#themes-are-degenerate-single-entities)). Some customers may *be* a theme (e.g. when tracking "mobility in Querétaro" rather than a named entity). Decide whether the customer slot accepts theme rows. |
| 7 | **Multi-stance per source item?** | Start with one. The LLM prompt and the table layout should be forward-compatible (e.g. accept a list, even if length 1). Multi-claim per source item is allowed from v1. |
| 8 | **Claim retention threshold** | A claim is retained only if `customer ∈ affected_entities`. Should "the customer is mentioned in the article but not the alleged fact" count? Probably no — keep the rule strict, drop ambiguous ones. |
| 9 | **Article claims vs user claims** (decided) | Same row layout for both, distinguished by `source_kind` (`article` / `user_post` / `user_comment`) — mirrors how `userdb.entities_documents_sentiments_org` carries both posts and comments via `doc_index`. They share clusters at the catalog level; downstream processing weights or splits them by `source_kind` as needed. |
| 10 | **Claim novelty / verification** (decided: novelty over verification) | We don't fact-check claims. We flag **new** ones (per-event in v1, globally once cross-event linking lands), pairing with `importance` to surface "things the customer should know about now". See [New-claim flagging](#new-claim-flagging). Full verification stays out of scope. |
| 11 | **Cross-event claim graph (later)** | Linking claim clusters across events when the same allegation recurs (e.g. "AXA delayed payouts" appearing in 5 different state-level events). v1 keeps clusters event-local; cross-event linking will likely run on embedding similarity over canonical phrasings since we don't carry parsed subject/predicate triples. |
| 11a | **Claim importance scale: 1–3 vs 1–5 vs multi-dim** | Starting with 1–3 scalar + one-sentence reason. A finer scale or a multi-dimensional score (reputational / operational / legal / scale) may help once we see real distributions. Revisit after the first slice. |
| 11b | **Importance calibration across batches and events** | The same claim may get different scores in different batches (LLM stochasticity) and "high" means different things in small vs large events. Decide whether to recompute on cluster merges, normalise within event, or accept drift and rely on the cluster aggregates. |
| 12 | **Schema modify vs replicate** for `entities_documents_sentiments_org`? | Stage 2 decision. |
| 13 | **Where do the two catalogs live in the DB?** | Stance catalog likely on `kgdb.entities.metadata` on the customer row; the per-event claim catalog is implicitly the set of `claim_clusters` rows for that `(customer_id, event_id)`. Stage 2 decision. |
| 14 | **Adjudicator scope (stances)** | Does the adjudicator see the whole catalog or only neighbours of the proposed change? Affects cost and quality. |
| 15 | **Cross-customer catalog reuse?** | Two customers in the same sector (two governments, two insurers) may share many stances. Worth seeding new customers from a sector-level template? Or do customers diverge enough that fresh-start is cleaner? |

## Next steps

1. **Architecture plan** → produce [`tags_impl_plan.md`](tags_impl_plan.md) — heavily object-oriented, naming the classes and main functions for Stage 1 (in-memory). Should cover: customer / content-graph configuration, retrieval class, stance catalog class (per customer), claim catalog class (per `(customer, event)`, made of clusters), tagging orchestrator (emits both stances and claims in one pass), stance adjudicator, claim clusterer, persistence interface (with a no-op default for Stage 1).
2. **First slice** — pick one customer (likely the Government of Querétaro, where we already have linked content from `data/ayuntamiento_qro/`), define its content graph, and run **Phase 1** (stance catalog bootstrap) end-to-end on a corpus that spans multiple events/themes touching it. Validate that the resulting catalog reads as customer-anchored (enduring qualities), not event-anchored (specific complaints).
3. **Second slice** — wire up **Phase 2** so a single batch yields both stance assignments and structured claims (with `importance` + `importance_reason`) for one event, then **Phase 4** clustering on that event's claims. Eyeball both outputs against the same corpus to confirm the split feels right (stances stable across events, claims rich within them) and check that the importance scores roughly align with what a human reader would flag as "the customer should know about this".
4. **Iterate** — once Phase 2 + 4 stabilise, layer in Phase 3 (stance adjudication) and tune. Track:
   - how many proposed stance mutations the adjudicator generalises (signal: prompt quality for stances),
   - how often new raw claims map to existing clusters vs spawn new ones (signal: clustering aggressiveness).
   Defer Stage 2 persistence until both pipelines are proven.

## Pointers

- KG database schema and persistence model: [`media-backend-paid/docs/DATABASE_POSTGRES.md`](../../../../../media-backend-paid/docs/DATABASE_POSTGRES.md)
- Linker docs (events as entities, themes as degenerate entities, target write path): [`../linking/readme_linking.md`](../linking/readme_linking.md)
- PoC pipeline (stances only, event-scoped): [`rrss_pg/readme_stances_pipeline.md`](../../../../../rrss_pg/readme_stances_pipeline.md)
- Entities overview: [`../readme_entities.md`](../readme_entities.md)
