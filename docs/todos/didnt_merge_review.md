# Review — precision-6/7 records that did NOT merge deterministically

**Status:** review (findings, not yet actioned)
**Source:** `data/.runlogs/linking_cases_geo_qro_public_works_event__detslack.jsonl` (the
Querétaro public_works run with the level-6/7-share deterministic gate), joined with each
record's geocode (`precision_level`, `level_6_id`, `level_7_id`).
**Related:** [`deterministic_match_slack.md`](deterministic_match_slack.md), [`location_level_list_extraction.md`](location_level_list_extraction.md), [`../../src/entities/linking/readme_linking.md`](../../src/entities/linking/readme_linking.md)

## Why this review

The deterministic gate fires only on shared `level_6_id`/`level_7_id` + **extracted**-date
overlap. So a record can have **fine geo (precision 6/7) and still skip the gate**. Those
are the interesting cases: either the gate *correctly* abstained (and the LLM then did
something worth checking), or the gate *should* have caught it but a guard blocked it.

**14 such records** in this run — **7 `no_candidates`** (created, nothing to match) and
**7 `llm`** (had candidates, went to the LLM). Four patterns below.

## Pattern A — shares a level-6 street, but deterministic abstained on a **publication-only date**

The gate requires an *extracted* date on the incoming record; incident reports usually
carry only the article timestamp, so they fall to the LLM **even when they share the exact
street**. Example — three sinkhole reports on **Paseo de Belgrado**, colonia Tejeda
(`level_6_id=_484220060001004200002`):

- *"Socavón detectado en … Paseo de Belgrado … deterioro de un parche de concreto"* — `date_source=publication` → **llm** → `created` (a new event, *not* merged into the earlier Belgrado sinkhole 357.9 m away).
- *"Deterioro de un parche de concreto hidraulico en la calle Paseo de Belgrado"* — `publication` → **llm** → merged.

They share `level_6_id` and the same day, so the gate's *geo* condition holds — only the
extracted-date guard blocked it. **Review question:** should a shared `level_6_id`/`level_7_id`
permit a **publication-date** overlap (relax the extracted-date requirement) when geo is that
precise? Trade-off: that is exactly the *same-street-collision* weakness (two distinct
incidents on one street/day) — but at level 7 (place) it is low-risk.

## Pattern B — the LLM **over-merged across different fine streets** (gate would not have)

The deterministic gate keys on shared fine ids, so it would never merge two *different*
streets. The LLM did:

- **Paseo de México sinkhole** (`level_6_id=_484220060001006800029`) was **merged by the LLM**
  into the **Paseo de Belgrado** cluster (`…004200002`) — different streets. The
  *"Cierre de dos tramos … uno en Paseo de México y otro en Paseo de Belgrado"* article makes
  explicit these are **two** distinct socavones. Likely **LLM over-merge**; the gate's
  street-level discrimination is *more correct* here.
- A Santa María Magdalena `infrastructure` record was LLM-merged into a candidate **1,748 m
  away** (`geo_dist_m=1748.6`, both nameless) — far apart, plausibly distinct works.

**Review question:** constrain the LLM with the level signal (don't merge when fine ids
differ beyond a threshold?), or treat these as the audit's confirmed over-merges.

## Pattern C — one multi-street project, each record a **different single street** → no shared id → fragmented

The Santa María Magdalena "10 calles" rehabilitation surfaces as several precision-6 records
that each geocoded to a *different* `level_6_id` (e.g. `…035100135`, `…017100004`) and so share
nothing — created separately (`no_candidates`):

- *"Construccion y pavimentacion de 10 calles en la colonia Santa Maria Magdalena…"*
- *"Intervencion integral de las calles San Juan del Rio y Amealco … en la comunidad de Guadalupe La Venta…"*

This is the [`location_level_list_extraction`](location_level_list_extraction.md) case at
*fine* precision: each article picked **one** street, so the records don't share an id even
though they're the same corridor project. List-valued locations would let them share a street.

## Pattern D — genuine solos (no candidate; correctly created)

Fine-precision records with no same-type/place/day candidate — nothing to review:

- **`Tercera Carrera con Causa`** (`sports_event`, precision **7**, Bosque de Chapultepec — note `level_6_id` starts `_48409` = CDMX, a non-Querétaro event mentioned in a Querétaro-tagged doc).
- **`Rehabilitacion Integral de la Avenida Candiles`** (`paving`, named, precision 6).
- **Peñuelas `water_issue`** (hundimientos tras lluvias, precision 6).

## Suggested actions

1. **Pattern A — done.** The deterministic gate now accepts a **publication-date** overlap when
   the shared id is at a leaf level (`det_publication_levels=(7,)`, level 7 / place). *Observed:*
   no effect on this fixture — its fine sinkholes resolve to **level 6** (street), not 7, so the
   level-7 relaxation didn't fire. Widening to `(6,7)` would catch them but amplifies the
   same-street weakness.
2. **Pattern B — done (soft), insufficient.** Each candidate now carries `ubicacion_fina`
   (`misma`/`distinta`/`null`) and the prompt treats `distinta` as different. *Observed:* the LLM
   **overruled** it — the México↔Belgrado sinkholes stayed merged. The hard variant (skip the LLM
   entirely on a leaf disagreement) is needed to actually prevent it; assessed in
   [`skip_llm_on_leaf_disagreement.md`](skip_llm_on_leaf_disagreement.md) (gated on geocoder
   leaf accuracy).
3. **Pattern C** is tracked by `location_level_list_extraction.md`; no separate action.
