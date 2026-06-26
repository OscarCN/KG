# TODO — Canonical↔canonical reconciliation (consistency pass & multi-match merge)

**Status:** open — design + prototype
**Area:** `src/entities/linking/strategy.py`, `src/entities/linking/link.py`, `src/entities/linking/index.py`
**Related:** [`retrieval_linking_per_supertype.md`](retrieval_linking_per_supertype.md), [`location_level_list_extraction.md`](location_level_list_extraction.md), [`../linking.md`](../linking.md)

## Problem

The linker only ever merges an **incoming record into one existing canonical event**. It
never merges two **already-canonical** events together, and `adjudicate()` returns the
**first** matching candidate (the deterministic gate stops at the first shared-leaf+date
hit; the LLM returns a single `match_id`). So once two canonical events for the same
real-world occurrence exist, nothing reconciles them — they are a permanent twin.

**Evidence (geo_qro_paid_mass_event run, 2026-06-16).** "Zona Fest" — one festival at
Estadio Corregidora (~Jun 11–Jul 19) — fragmented into ~18 canonical events. Two of them,
`festival` 586469 (13 sources) and `festival` 445112 (15 sources), share the **identical**
`level_7_id` (`_4842201400010181000020001`) and both start 2026-06-11, yet stayed separate.
They forked because 445112's *seed* source carried a misparsed date (2026-01-06,
`precision_days=42`) whose window didn't overlap 586469 at creation; later 06-11 sources
then merged arbitrarily into one twin or the other (set-iteration order). A coarse/misdated
seed forks a cluster, and nothing ever heals it.

This is **orthogonal to** the supertype-partition change (which addresses fragmentation
*across* `event_type`s — see [`retrieval_linking_per_supertype.md`](retrieval_linking_per_supertype.md)).
Even with a perfect partition, the twin leak remains.

## Two complementary directions to explore

### A. Multi-match merge at link time

When an incoming record matches **more than one** existing canonical event, treat that as
evidence those canonical events are themselves the same, and **merge all of them** (fold the
incoming record + every matched canonical into one survivor).

- Change `adjudicate()` to return the **full set** of matching candidate ids, not just the
  first: the deterministic gate collects *every* candidate sharing a leaf id + date; the LLM
  prompt allows `{"match_ids": [...]}`.
- `link.py` then merges the incoming record into a chosen survivor **and** folds the other
  matched canonicals into it.
- Needs a real **canonical-merge primitive** (`merge_events(into, *others)`): union
  `source_ids` / `_source_windows`, pick the best `date_range`/`location`, and **re-point the
  index** — every key the absorbed events were registered under must now resolve to the
  survivor (or be rewritten). The current `CandidateIndex` is append-only key→ids; absorbing
  an event means either rewriting ids or adding an alias layer (`current_entity_id`-style,
  mirroring `entities_alias` in kgdb).
- Catches twins **the moment** a bridging record arrives — but only if such a record arrives
  and matches both. Doesn't heal twins that no single later record bridges.

### B. Periodic consistency pass (offline reconciliation)

Run a sweep over the current canonical set (every N records, end-of-batch, or scheduled) that
finds canonical events that *should* be one and merges them — independent of any new
incoming record.

- Candidate generation: same partition keys the linker already builds (supertype/event_type,
  geo keys, date windows), but **candidate-vs-candidate** instead of incoming-vs-candidate.
- Decision: reuse the deterministic gate (shared `level_6/7_id` + date overlap) and/or an LLM
  adjudication pass over the canonical pair.
- Same `merge_events` primitive + index re-pointing as (A).
- Heals twins regardless of whether a bridging record ever arrives (would fix the 586469 /
  445112 pair directly). Cost: a periodic O(candidates) sweep; cadence is a tuning knob.

A and B share the hard part — the **canonical-merge primitive + index re-pointing / alias
layer**. Build that once; A calls it inline, B calls it in a sweep.

## Open questions

- **Index re-pointing vs. alias indirection.** Rewrite registered ids on merge, or add a
  `current_id` alias map (cheaper, mirrors kgdb `entities_alias.current_entity_id`)? The alias
  route lines up with the eventual kgdb persistence model.
- **Transitivity / merge storms.** Multi-match can chain (A≡B, B≡C ⇒ all one). Bound it; make
  sure one over-eager bridge can't collapse a whole partition (the single-state degeneracy in
  a new guise).
- **Cadence for the consistency pass.** Every N documents, per-batch, or scheduled? Streaming
  (A) + occasional sweep (B) is likely the right combination.
- **Interaction with deterministic-gate weaknesses.** Same-street (level 6) collisions and the
  publication-date leaf rule get *amplified* by canonical-canonical merging — the pass must be
  at least as conservative as the per-record gate (lean on level 7; defer level 6 to the LLM).

## Validation

- Re-run `geo_qro_paid_mass_event`; the 586469 / 445112 festival twins must collapse, and the
  Zona Fest cluster count must drop, **without** over-merging distinct stadium events (e.g. the
  individual World Cup matches, or `detention` 676105 at the same venue).
- Compare canonical count + multi-source-event count before/after; inspect the case log for any
  newly introduced over-merges.
