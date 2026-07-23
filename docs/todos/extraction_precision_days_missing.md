# TODO — Extraction rarely emits `precision_days` (80% null), muddying date narrowness

**Status:** open — diagnosed; merge-side mitigated, extraction fix not started
**Area:** `src/entities/extraction/` (prompts + `DateRangeFromUnstructured` composite type)
**Related:** [`extraction_date_daymonth_swap.md`](extraction_date_daymonth_swap.md), [`canonical_reconciliation.md`](canonical_reconciliation.md), [`../linking.md`](../linking.md)

## Problem

`precision_days` on the extracted `date_range` is the field that tells the linker how
*wide* an approximate date is ("en marzo" → ~30, "en 2026" → ~365). The linker uses it
to (a) widen the retrieval window and (b) pick the **most precise** window when merging
sources into a canonical. But **~80% of extracted events (1798 / 2246 in dev kgdb) carry
`precision_days = null`** — the LLM collapses a vague mention to a point start date (often
`YYYY-01-01`) and omits the precision.

Examples (all `precision_days = null`):

| mention | extracted start..end |
|---|---|
| `enero de 2025` | `2025-01-01` .. — |
| `en lo que iba de 2026` | `2026-01-01` .. — |
| `por segundo año consecutivo` | `2026-01-01` .. `2026-05-18` |
| `periodo 2023-2027` | `2023-01-01` .. `2027-12-31` |
| `durante 2026` | `2026-01-01` .. `2026-07-04` |

A month/year/multi-year mention becomes an unqualified point (or a wide range with no
precision flag), indistinguishable from a genuinely precise single-day date.

## Merge-side mitigation (done)

`GeoEventStrategy._apply_best_window` no longer treats `precision_days = null` as *exact*
(the old `min(..., key=lambda w: w["precision_days"] or 0)` ranked null as `0` — most
precise, so a vague "durante 2026" beat a real dated sibling and collapsed the canonical).
It now ranks by `_effective_precision_days`: explicit `precision_days`, else the window
**width** (`end - start`), else `inf` for a start-only unknown. Records that carry a wide
`end` now rank correctly wide; but a **start-only** vague date (no `end`, no `precision_days`)
is still unrecoverable at merge time — hence the upstream fix.

## Extraction fix (the real one)

Make extraction **always populate `precision_days`** consistent with the mention's
granularity:
- day → 0/1, month → ~30, quarter/season → ~90, year → ~365, multi-year → span in days.
- When the mention is a period, also emit the `end` (so width is recoverable even if
  `precision_days` is dropped).
- Tighten the `DateRangeFromUnstructured` field description + the generated-prompt rules and
  worked examples; verify against the mentions above.

Couples with the [day↔month swap](extraction_date_daymonth_swap.md) fix — both are
`date_range` extraction-quality bugs and should be validated together (re-extract the
affected articles, re-check `start`/`end`/`precision_days` against the `mention`).

## Validation

- Census `precision_days IS NULL` share before/after (target: ≪ 80%).
- Spot-check that month/year mentions get proportional `precision_days` and a populated `end`.
