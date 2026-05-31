# TODO — Per-supertype retrieval & linking strategy

**Status:** proposed (not implemented)
**Area:** `src/entities/linking/`
**Related:** [`src/entities/readme_entities.md`](../../src/entities/readme_entities.md), [`src/entities/linking/readme_linking.md`](../../src/entities/linking/readme_linking.md)

## Problem

What makes an entity **identifiable** — and therefore how we retrieve linking
candidates and decide whether two records are the same — differs by ontology
class. The current linker (`src/entities/linking/link.py`) hardcodes a single
strategy that only fits **events with a clean geographic component**:

- **Candidate index:** `(event_type, level_2_id, date_key) → {event_ids}`
- **Disambiguation:** one LLM call over `name`, `description`, structured
  `address`, `date`.

This breaks down for entity types whose identity does not hinge on geography or
on a date:

| Supertype (example) | Identifying attributes | Why the current strategy fails |
|---|---|---|
| **events with location** (current) | class + location + date-time | — (this is the implemented case) |
| **persons / organizations / products** | name and/or description | no meaningful date or location to filter on |
| **legislative_initiative** | region (location) + name + description | location is jurisdiction, not an incident point; name/description carry most of the identity |
| **real estate developments** | location + name | no date; dedup is name-at-a-place |
| **person's actions** — `said` (statement/claim/endorsement/assertion) | **actor** + description (claim content) + date-time | no geo; dedup is *who said it* + *what was said* + *when* |
| **person's actions** — `did` (attended/organized/gave-interview) | **actor** + action-class + date-time + *optional* location | geo is weak/optional; actor identity is the primary key |

Two cross-cutting constraints make a naive "embed everything + LLM decide"
approach unworkable:

1. **Candidate retrieval must match the attribute.** Embeddings retrieve poorly
   on geospatial data and on uncommon/proper names. Geo wants a spatial index,
   names want LSH/token overlap, free text wants embeddings, and resolved
   entities (e.g. a person `actor_id`) want exact-match. One method does not fit
   all attributes.
2. **LLM disambiguation is costly.** It should run only after a cheap,
   high-precision candidate filter has shrunk the set — ideally to a handful.
   For `person_actions`, an exact `actor_id` filter (the actor resolved to a KB
   person entity) is the cheap primary narrowing step before any embedding or
   LLM call.

## Goal

Define, **per supertype (and where needed, per class-family)**, a declarative
**retrieval strategy** and **linking strategy** expressed as a *set of
attributes + the method used on each*, so that:

- Adding or changing a supertype's linking behaviour is **configuration, not
  code**.
- The retrieval and linking logic is **modular** — a small set of reusable
  matchers (exact, date-overlap, geo, name-token/LSH, embedding-kNN) composed by
  the declared strategy.

## Scope

### v1 (do now)
- **Hardcode** the retrieval + linking strategy per supertype in code, but
  behind a **clean, modular interface** (a strategy object / registry keyed by
  supertype) so the call sites in `link.py` no longer assume the
  `(event_type, level_2_id, date_key)` shape.
- Preserve the current event behaviour exactly for geo-events.
- Add an `actor_id`-keyed strategy for `person_actions` (`said` / `did`).
- Do **not** build general-purpose retrieval infrastructure (vector DB, etc.)
  yet — `said` content-dedup can lean on the LLM disambiguator over a tiny
  actor+date-filtered candidate set.

### v2 (later)
- Generalize retrieval to a declared `(attribute → method)` spec read from the
  schema `meta` (e.g. `meta.linking` / `meta.retrieval`), so no supertype needs
  bespoke code.
- Introduce the embedding/vector retrieval path for description-driven types
  (persons, organizations, products) once the interface is stable.

## Design sketch

Each supertype (optionally narrowed by class-family) declares:

- **Retrieval** — ordered list of `(attribute, method)` used to build the
  candidate index and to look candidates up:
  - `exact` — e.g. `actor_id`, `level_2_id`, `event_type`
  - `date_overlap` — slack-expanded day-key overlap (already implemented)
  - `geo` — spatial proximity / containment
  - `name_token` / `lsh` — fuzzy name match (Redis LSH)
  - `embedding_knn` — semantic nearest-neighbour on `description` (v2)
- **Disambiguation** — whether an LLM call is needed and **which fields** to
  present to it; with deterministic short-circuits (e.g. single candidate with
  exact id + date + type ⇒ accept without LLM).

Proposed home for the declaration: the supertype schema `meta` block, mirroring
how `meta.category` already drives extraction routing — keeping linking
declarative and code-free. Class-family overrides (the `said` vs `did` case)
need the spec to support a per-class layer on top of the supertype default.

### Strategy table (target)

| Supertype / family | Retrieval (candidate filter) | Disambiguation fields |
|---|---|---|
| geo events (current) | `event_type` (exact) ∧ `level_2_id` (exact) ∧ `date_overlap` | name, description, address, date |
| `legislative_initiative` | `entity_type` (exact) ∧ `level_2_id`/region (exact) ∧ `name_token` | name, description, jurisdiction |
| persons / orgs / products | `entity_type` (exact) ∧ `name_token`/`lsh` (+ `embedding_knn` in v2) | name, description |
| real estate developments | `level_2_id` (exact) ∧ `name_token` | name, location |
| `person_actions` · `said` | `actor_id` (exact) ∧ `date_overlap` | actor, claim/description (content), date |
| `person_actions` · `did` | `actor_id` (exact) ∧ `date_overlap` ∧ *optional* `level_2_id` | actor, action class, date, location |

## Open questions

- **Where does the strategy live** — schema `meta`, a sibling config file, or a
  code registry? (Leaning `meta` for consistency with `meta.category`.)
- **Class-family overrides:** the current linker keys strategy by supertype.
  `said`/`did` proves we need a per-class layer. How granular — class-family, or
  per-class?
- **`actor_id` resolution:** `person_actions` linking presumes the actor is
  already resolved to a KB **person** entity during extraction/linking. That
  dependency (person-as-entity + actor field on the action schema) must land
  first. See the `person_actions` / stream-task work.
- **Cross-source `said` dedup without geo:** with no geographic narrowing,
  statement dedup rests on `actor_id` + date + semantic match of content. Verify
  recall is acceptable on a small actor+date candidate set before deciding
  whether v2 embeddings are needed here.
- **Cost guardrails:** define the deterministic short-circuits that let us skip
  the LLM call entirely (single exact-keyed candidate, etc.).

## Acceptance (v1)

- `link.py` no longer hardcodes the `(event_type, level_2_id, date_key)` index
  shape at its call sites; strategy is selected per supertype via a registry.
- Geo-event linking output is unchanged (regression-checked against existing
  `data/linked/` fixtures).
- `person_actions` (`said` / `did`) links by `actor_id` + date with the correct
  disambiguation fields.
- README and entity docs point here; behaviour documented in
  `readme_linking.md` once implemented.
