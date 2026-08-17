# TODO — Best-window merge lets a narrow sub-event window overwrite an exact multi-day span

**Status:** open — diagnosed 2026-08-12 (traced from DeepRiver consumer side; 5 confirmed cases, 33 affected rows live)
**Area:** `src/entities/linking/strategy.py` (`_apply_best_window`), `src/entities/linking/aggregate.py`
**Related:** [`merge_best_window_blocks_end_dates.md`](merge_best_window_blocks_end_dates.md) (sibling failure mode of the same rule), [`extraction_precision_days_missing.md`](extraction_precision_days_missing.md), [`canonical_reconciliation.md`](canonical_reconciliation.md)

## Problem

`_apply_best_window` sets the canonical `date_range` to the single **narrowest**
extracted window (`effective_precision_days`: explicit `precision_days`, else window
*width*). That conflates **duration with uncertainty**: an exact stated range
(«Del 6 al 16 de agosto» — both endpoints known, but `precision_days` null → effective
precision = width = 10) loses to *any* later single-day window. And for long
festivals/fairs, single-day windows keep arriving — articles about one *función* within
the run get extracted with that day as the umbrella event's `date_range`.

Five confirmed live cases (canonical range vs the wide window sitting right there in
`_source_windows`, which the surviving canonical `mention` still quotes):

| entity | event | canonical | widest ledger window / mention |
|---|---|---|---|
| 452 | FIDCDMX | Aug 9→10 | «Del 6 al 16 de agosto» |
| 331 | Fiesta de las Culturas Indígenas | Aug 9→10 | «del 7 al 23 de agosto» |
| 354 | Taboo Sin Censura | Oct 16 | «del 15 al 18 de octubre» |
| 318 | Vacaciones de Verano | Aug 6→7 | «16 jul – 30 ago» |
| 3542 | Feria de San Francisco Pachuca | Oct 2→3 | «25 sep – 19 oct» |

Consumer impact: the cartelera/map day-window queries miss currently-running festivals
(452 and 331 were both live and invisible on 2026-08-12). A sweep (ledger has an
extracted two-endpoint window ≥3 days wide with `precision_days` ≤7-or-null, canonical
≤1 day) finds **33 affected rows** of 3826 with ledgers.

Two aggravators, tracked elsewhere:

- **`precision_days` is null on ~80% of windows** ([`extraction_precision_days_missing.md`](extraction_precision_days_missing.md)),
  so the width-fallback — the buggy conflation — decides almost always. (Note it is
  *not* enough to fix emission: an exact range and an exact single day would then both
  rank 0 and only the earliest-seen tiebreak would save the span.)
- **Sub-event granularity leaks** (per the granularity policy, umbrella and sub-events
  are separate canonicals — see the layer notes in [`canonical_reconciliation.md`](canonical_reconciliation.md)): kg even minted entity 453 for the FIDCDMX galas separately, yet
  gala-day windows still landed in 452's ledger via other articles. The merge policy
  must be robust to per-day mentions of an umbrella regardless.

The metadata is also left self-inconsistent: `_apply_best_window` overwrites
`start`/`end`/`precision_days` but not `mention`, so the canonical quotes «del 7 al 23
de agosto» while its range says Aug 9. (Useful for detection/repair; still a bug.)

Note the sibling TODO's direction (pick start and end **independently**) does *not* fix
this mode: the winning single-day window carries both a precise start and a precise end.

## Direction

`aggregate.py` already has the right concept: `aggregate_date(windows, layer="umbrella")`
→ envelope of the dominant cluster. The linker never uses it — `_apply_best_window`
always applies the `instance` (narrowest-wins) rule. Sketch:

1. **Infer the layer at merge time** from the ledger: if the dominant cluster contains a
   two-endpoint extracted window ≥N days wide with small-or-null `precision_days` that
   (near-)contains most other windows → treat as `umbrella`. (Event-type hints —
   festival/fair/exposition — can strengthen but shouldn't be required.)
2. **Umbrella canonical = best observed container**, not the raw envelope: among ledger
   windows, pick the one *containing* the most other windows; tie-break by smaller
   `effective_precision_days`, then earliest-seen. Preferring an observed window over
   the min/max envelope (a) keeps `mention` coherent with the range and (b) stops a
   vague «en agosto» (prec 30, Aug 1–31) or an outlier start from inflating the span —
   e.g. 452's ledger picks «Del 6 al 16» over the envelope Aug 1–16.
3. **Instance rule unchanged** for point events (the common case), including the
   sibling TODO's independent-end fix.
4. **Carry `mention` (and window provenance) into `_source_windows`** so the winner's
   mention can be promoted along with its range.
5. **Repair pass** for the 33 affected rows: recompute from `_source_windows` under the
   new policy and re-persist `event_properties.event_date_*` (the ledger is intact, no
   re-extraction needed). Detection query = the sweep above, or mention-vs-range
   disagreement.

Regression tests: exact multi-day span + later single-day sub-event window ⇒ canonical
keeps the span; vague month window never becomes the canonical; point event + noisy
duplicate days ⇒ narrowest still wins.
