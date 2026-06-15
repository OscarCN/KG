# TODO — Assess: skip the LLM (deterministic non-merge) when leaf locations disagree

**Status:** open — assessment, gated on geocoder quality
**Area:** `src/entities/linking/strategy.py` (deterministic gate / `_llm_adjudicate`)
**Related:** [`didnt_merge_review.md`](didnt_merge_review.md), [`deterministic_match_slack.md`](deterministic_match_slack.md), [`../../src/entities/linking/readme_linking.md`](../../src/entities/linking/readme_linking.md)

## Idea

Today, when an incoming event and a candidate resolve to **different leaf locations**
(different `level_6_id`/`level_7_id`), we still send the pair to the LLM with a *soft*
negative signal — the `ubicacion_fina="distinta"` field (Pattern B, implemented). The
LLM can still overrule it and merge (and it did over-merge the Paseo de México ↔ Paseo de
Belgrado sinkholes — see `didnt_merge_review.md`, Pattern B).

The aggressive variant: **treat a leaf disagreement as a hard non-match — skip the LLM
call entirely** for that candidate. If two records geocode to *different specific
streets/places*, declare them different events without asking the LLM.

This would have prevented the México↔Belgrado over-merge, and it's cheaper (fewer LLM
calls). But its correctness rests entirely on **geocoder leaf accuracy**.

## Why it's gated on the geocoder

The hard skip is only safe if "different `level_6_id`/`level_7_id`" reliably means
"different real place." The failure mode is a **recall loss**: if the geocoder assigns
*different* leaf ids to two mentions of the **same** place (spelling variants, abbrev.,
one mention naming the street + another the POI, centroid drift, the known
highest-`precision_level`-wins quirk), the hard skip would wrongly keep them apart with no
LLM fallback to recover. The soft signal (current) tolerates this; the hard skip does not.

The geocoder is being actively improved (a more performant version is expected), so leaf
consistency should rise — which is exactly what would make this reasonable. This TODO is
to **measure that consistency and decide**, not to implement blind.

## Assessment plan

1. **Measure geocoder leaf consistency.** On a labelled or eyeballed set of same-event
   record pairs (e.g. the multi-source clusters already in `data/linked/`), how often do
   two mentions of the *same* place get the **same** `level_6_id` / `level_7_id`? Break
   down by level (7 vs 6) — level 7 (POI) is likely more stable than level 6 (street).
2. **Quantify the upside.** From the case log, count LLM calls where `ubicacion_fina=
   "distinta"` and the LLM merged anyway — confirmed over-merges avoided vs. correct
   merges that would be lost.
3. **Decide per level.** A hard skip at **level 7** may already be safe; **level 6** may
   need to wait for the better geocoder. Likely ship as a param
   (`det_hard_nonmatch_levels: Tuple[int,...]`, default `()` = off) and enable level 7
   first.

## Decision criteria

- Enable the hard skip at a level **iff** geocoder same-place leaf-consistency at that
  level is high enough that the recall loss is < the over-merge avoided (net precision and
  recall both improve, or precision improves at negligible recall cost).
- Re-run the public_works (and a named/venue) fixture; the México↔Belgrado pair must split,
  and no genuine multi-source cluster may fragment due to leaf-id noise.

## Notes

- Independent of, and complementary to, [`location_level_list_extraction.md`](location_level_list_extraction.md):
  list-valued locations make leaf **agreement** more common (records share *a* street),
  while this TODO is about trusting leaf **disagreement**. Both get better as the geocoder
  does.
- Keep the soft `ubicacion_fina` signal regardless; the hard skip would sit *in front of*
  it (skip the LLM on disagreement) rather than replace it.
