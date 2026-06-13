# Deep River — Value Offer & Capability Map

Working document grounding (1) the product/market opportunities for the landing
page and (2) the skills/positioning narrative for Oscar's CV. Both are built on
the same engine described across this repository.

---

## 1. The one-liner

> **Deep River turns the noise of news and social media into a structured,
> geolocated, real-time knowledge graph of what is happening in the world — and
> what people think about it.**

We don't track *mentions*; we extract *facts*. Every event, entity, and concept
is typed by an ontology, resolved to a precise location, deduplicated across
sources into a single canonical record, and enriched with the public opinion
attached to it — then exposed as a queryable, real-time **facts feed**.

---

## 2. The engine (what actually powers it)

| Capability | What it does | Why it's hard / defensible |
|---|---|---|
| **Knowledge acquisition** | LLM-based structured extraction of events, entities/concepts, and themes from unstructured/semi-structured text (news, social posts, comments), driven by a declarative ontology (keyword → class → supertype → schema). | Schema-driven, auto-generated extraction prompts; extensible to new domains by configuration, not code. |
| **Geocoding (core differentiator)** | A **custom, trainable geocoder built from scratch** that resolves events and entities to **street- or venue-level coordinates**, backed by a growing proprietary locations dataset. | Off-the-shelf geocoders fail on informal, Spanish-language, place-name-rich text; ours improves with use. This is the moat. |
| **Entity linking / canonical facts** | Deduplicates the same event/entity reported across many sources into one canonical record carrying every source. Per-supertype retrieval + linking strategies (geo, name/LSH, semantic, exact-id). | The same protest reported by 20 outlets becomes **one fact**, not 20 mentions. |
| **Opinion modeling** | Customer/subject-anchored **stances** (durable attitudes) and per-event **claims** (specific assertions), with freshness and importance — opinion tied to *specific events and actions*, not aggregate brand buzz. | Answers "what do people think about *this* action," not just "is sentiment up." |
| **Facts feed / retrieval** | Everything is queryable in real time — by **coordinates** (specific places, routes, areas), by **individual entity** (a law initiative, a technology, a person), or by **type** (security, infrastructure, emergencies…). | Structured + geospatial + real-time + machine-consumable. A knowledge graph, not a dashboard. |

---

## 3. Why this beats traditional media monitoring / social listening

| Dimension | Social listening (Brandwatch, Meltwater, Talkwalker…) | **Deep River** |
|---|---|---|
| Unit of analysis | Keyword **mentions** | Structured **events & entities** (what/where/when/who) |
| Location | None, or city-level tags | **Street / venue-level coordinates** |
| Cross-source handling | Raw mention streams (20 articles = 20 rows) | **Deduplicated canonical facts** (20 articles = 1 fact, 20 sources) |
| Opinion | Aggregate sentiment on a brand/keyword | Opinion **tied to a specific event/action** (stances + claims) |
| Output | Dashboards & buzz volume | **Queryable knowledge graph / facts API** |
| Extensibility | Fixed brand/topic tracking | **Ontology-driven**, new domains by configuration |
| Consumer | Human analysts | Humans **and AI agents** (grounded, structured facts) |

The headline: **they measure conversation; we model reality and the opinion
about it.**

---

## 4. Markets / verticals

Each vertical is the same engine pointed at a different ontology + subject set.
Structure the landing page around these.

### 4.1 Government & public sector
**Who:** municipal/state governments, security & civil-protection agencies,
urban planning, public-works offices.
**Problem:** they learn about incidents and citizen discontent late, in
fragments, and can't measure perception of what they do.
**We provide:** automatic discovery, tracking, and measurement of **every event
in a city/region** — security, emergencies, infrastructure, public works,
protests, mobility — each mapped to a precise location, plus the **public
opinion behind it**.
**Differentiator:** a live, geolocated operating picture of the city *and* its
sentiment, without waiting for citizens to file reports.
**Example queries:** "all security incidents this week within this district,"
"citizen sentiment about the new public-works program by neighborhood,"
"emerging infrastructure complaints along this avenue."

### 4.2 Political campaigns & public figures
**Who:** campaigns, political consultancies, government communications offices.
**Problem:** they react to polls and aggregate sentiment, with no fast,
per-action feedback on messaging and behavior.
**We provide:** tracking of **every action and statement** a candidate makes and
the **public reaction to each one individually** — a tight feedback loop to tune
actions, stance, and messaging. Plus opponent tracking and issue-by-geography
mapping.
**Differentiator:** per-action feedback (not a monthly sentiment score), grounded
in deduplicated facts and stance/claim modeling.
**Example queries:** "reaction to yesterday's statement on water policy,"
"which districts react negatively to this stance," "how is this promise being
re-framed across media."

### 4.3 Marketing, brands & agencies
**Who:** brands, marketing/PR agencies, market research, competitive
intelligence.
**Problem:** campaign and reputation measurement is buzz-volume and coarse
sentiment, disconnected from concrete events.
**We provide:** tracking of **marketing campaigns and the public reaction**;
identification of **concerns, questions, and opinions** about products and
companies; tracking of **specific events affecting a company** (launches,
outages, recalls, controversies) and the reaction to each.
**Differentiator:** event-anchored insight (which *event* moved perception,
where, among whom) instead of undifferentiated mention sentiment.
**Example queries:** "top public concerns about this product line this month,"
"reaction to the launch event by region," "questions consumers keep asking about
this company."

### 4.4 Location intelligence *(emerging — leverages the geocoder moat)*
**Who:** real estate, retail site selection, insurance, logistics.
**Problem:** location risk/opportunity signals are static and stale.
**We provide:** events and entities (security, infrastructure, developments,
disruptions) resolved to coordinates → a **live event layer over the map** for
area scoring.
**Differentiator:** the geocoder makes every fact a spatial signal — routes,
zones, and points, updated in real time.

### 4.5 Facts-as-a-service / agent grounding *(horizontal platform)*
**Who:** AI products and agents, data teams, downstream applications.
**Problem:** LLMs/agents need **fresh, structured, grounded** real-world facts;
the open web is unstructured and unreliable.
**We provide:** the **facts feed** — a real-time, queryable, structured world
model (by geography, entity, or type) that agents consume directly.
**Differentiator:** this is the platform layer under every vertical above — the
same knowledge graph, exposed as an API for machine consumers.

---

## 5. CV — skills, areas, and positioning

The project is a textbook end-to-end **knowledge acquisition → modeling →
retrieval** system. Frame it that way; it reads as senior, system-level work and
transfers cleanly to enterprise knowledge / AI-grounding roles.

### 5.1 Areas of interest (headline)
Knowledge acquisition, representation & retrieval · Knowledge graphs ·
Information extraction / applied NLP & LLMs · Geospatial ML · Retrieval-augmented
generation & agent grounding.

### 5.2 Skill clusters (with the evidence this project provides)

- **Knowledge acquisition (information extraction):** ontology-driven extraction
  of events, entities, and themes from unstructured/semi-structured text using
  LLMs; schema-driven, auto-generated extraction prompts; multi-stage
  match → classify → extract pipelines.
- **Knowledge modeling / representation:** declarative schema & type system;
  ontology design (supertypes, classes, inheritance, composite types);
  knowledge-graph data modeling; entity–relationship and taxonomy design.
- **Knowledge retrieval:** entity linking, resolution, and deduplication;
  per-type candidate retrieval (geospatial indexing, LSH fuzzy matching,
  semantic/embedding, exact-key); building a queryable, real-time structured
  facts feed for downstream consumers (incl. AI agents / RAG grounding).
- **Geospatial ML (signature project):** designed and built a **custom,
  trainable geocoder from scratch** resolving informal text to street/venue-level
  coordinates, improving with a growing locations dataset.
- **Applied AI engineering:** LLM orchestration, prompt generation with
  feedback loops, caching/cost control, streaming pipelines, multi-database
  systems (Postgres/PostGIS, Redis, Elasticsearch, MongoDB).

### 5.3 Resume-ready bullets (adapt numbers as you measure them)

- Designed and built a **custom, trainable geocoding system from scratch** that
  resolves events and entities from informal Spanish-language text to
  **street/venue-level coordinates**, outperforming off-the-shelf geocoders on
  place-name-rich content.
- Architected an **ontology-driven knowledge-acquisition pipeline** that extracts
  structured events, entities, and themes from news and social media using LLMs,
  with schema-driven auto-generated extraction prompts (extensible to new domains
  by configuration, not code).
- Built a **declarative schema/type system and knowledge-graph model**
  (supertypes, class inheritance, composite types) backing a unified knowledge
  base.
- Implemented **entity linking and deduplication** with per-type retrieval
  strategies (geospatial, LSH, semantic, exact-key), collapsing multi-source
  reports into **canonical, deduplicated facts**.
- Delivered a **real-time, queryable "facts feed"** of geolocated world events
  and associated public opinion, queryable by **coordinates, entity, or type** —
  consumable by downstream applications and **AI agents needing grounded facts**.
- Modeled **public opinion as structured stances and per-event claims**, enabling
  per-action sentiment feedback rather than aggregate buzz.

### 5.4 Transferable framing (beyond the current domain)

- **Agent / AI grounding:** "retrieving facts from the world for agents that need
  fresh, structured, grounded knowledge" — i.e., a real-time RAG / tool layer.
- **Enterprise knowledge:** the same acquisition → modeling → retrieval stack
  applies to **structuring enterprise architecture and knowledge** (documents,
  systems, processes) into a queryable knowledge graph for search, analytics, and
  agent consumption.

---

## 6. Landing-page action items (derived from the above)

1. Lead with the **category reframe** (§1, §3): "we model reality and opinion,
   not conversation."
2. A **capabilities** strip from §2, with the geocoder as the named moat.
3. A **differentiator table** (§3) directly against social-listening incumbents.
4. **Per-vertical** sections (§4) — each with problem → what we provide →
   example queries.
5. A **"facts feed / API"** section (§4.5) for the platform/agent audience.
