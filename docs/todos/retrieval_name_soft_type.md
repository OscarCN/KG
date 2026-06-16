# TODO — Retrieval idea: hard date+location, soft name+type (retrieve by name too)

**Status:** open — idea / design
**Area:** `src/entities/linking/strategy.py` (candidate filter + adjudication), eventual kgdb retrieval (`event_properties`, `entity_locations`, `entities.name`)
**Related:** [`canonical_reconciliation.md`](canonical_reconciliation.md), [`retrieval_linking_per_supertype.md`](retrieval_linking_per_supertype.md), [`location_level_list_extraction.md`](location_level_list_extraction.md), [`../../src/entities/linking/readme_linking.md`](../../src/entities/linking/readme_linking.md)

## Idea

Invert which dimensions are *hard* (must agree to be a candidate at all) vs *soft* (evidence
the adjudicator weighs, but not a retrieval gate). Today the candidate filter is a strict
**conjunction** — same exact `event_type` **AND** same geo partition **AND** date overlap —
so any disagreement on type or partition makes two records invisible to each other.

Proposed:

- **Date — hard.** Confidence windows must overlap (range-scan on `event_properties.date_start
  /date_end`, which already store the slack-widened window).
- **Location — hard, but as *hierarchical compatibility*, not leaf-identity.** Two records are
  geo-compatible when their admin hierarchies don't contradict — same `level_2_id`, and finer
  ids are equal-or-one-refines-the-other (a `level_2`-only record is compatible with a
  `level_7` record in the same state; two *different* `level_6_id`s on the same street-pair are
  not). This is the key change for the coarse↔precise case.
- **Type — soft.** Don't require exact leaf `event_type`. Retrieve across types (at least within
  a supertype); let the deterministic gate / LLM treat a type difference as weak evidence, not a
  hard partition. (Subsumes [`retrieval_linking_per_supertype.md`](retrieval_linking_per_supertype.md).)
- **Name — soft *and* an extra retrieval path.** Add name-similarity retrieval (see *LSH vs
  trigram* below) so two records with the same name are candidates **even if** their type or
  geo precision differs. Name then also feeds adjudication as a positive signal.

So retrieval becomes: `(date overlap AND geo-compatible)` **OR** `(name-similar AND date
overlap)` → adjudicate the union with type+name as soft signals.

## Why it fixes the observed failures

- **Zona Fest fragmentation.** The coarse `441999` (Querétaro, level 2, type `festival`) and the
  precise `586469`/`445112` (Estadio Corregidora, level 7, types `festival`/`concert`/`party`)
  all share the name "Zona Fest" and overlapping dates, and are in the *same state* (geo-
  compatible, not contradictory). Under hard-type + leaf-geo they never meet; under this model
  they retrieve each other (by name + compatible geo + overlapping date) and adjudicate to one
  event. See the diagnosis in [`canonical_reconciliation.md`](canonical_reconciliation.md).
- **Multi-match.** Name retrieval naturally surfaces *several* existing canonical events for one
  incoming record (the cross-type/cross-precision variants), which is exactly the trigger the
  multi-match merge in [`canonical_reconciliation.md`](canonical_reconciliation.md) needs to
  collapse twins. The two ideas are complementary: this one *produces* the multi-match
  candidate set; that one *acts* on it.

## LSH vs trigram for name retrieval

- **Postgres `pg_trgm` GIN index on `entities.name`** (and/or `keywords`) — co-located with the
  data, nothing extra to keep consistent on merge, and it matches the char-trigram Jaccard the
  linker already uses (`text_util.name_similarity`). **Recommended first.**
- **LSH (Redis MinHash, as in the PoC)** — only worth the extra moving part (a second store to
  populate, and to fix up on every entity merge / alias repoint) at a scale where trigram GIN
  scans get slow. Defer until measured.
- **Embedding similarity** — `entities.embedding` exists (`numeric[]`); needs `pgvector` (or a
  vector index) to be a retrieval path rather than a rescoring step. Out of scope for v1.

## Risks / open questions

- **Recall blow-up / candidate cap.** Name + soft-type widens the candidate set; keep the
  `candidate_cap` and most-recent ordering, and make sure a generic name ("Zona Fest" is fine,
  but "Concierto" / "Partido" are not) doesn't pull in unrelated events. May need a name-
  specificity guard (skip name retrieval for low-IDF names).
- **Geo "hierarchical compatibility" needs a real definition.** Equal-or-prefix on `level_N_id`,
  decide how to treat a missing intermediate level, and what counts as "contradict" (two
  different `level_6_id`s under the same `level_5`). Leans on geocoder leaf accuracy — see
  [`skip_llm_on_leaf_disagreement.md`](skip_llm_on_leaf_disagreement.md).
- **Don't over-merge distinct same-name, same-venue, same-day events.** A stadium hosting two
  matches on one day shares name-fragments + geo + date; type/name being soft must not auto-
  merge them — cross-type/precise cases should still defer to the LLM rather than the
  deterministic gate.

## Validation

- Re-run `geo_qro_paid_mass_event`; the Zona Fest cluster count must drop sharply (the
  coarse/precise/cross-type variants collapse) **without** merging the individual World Cup
  matches at the same venue. Compare canonical + multi-source counts and inspect the case log.
