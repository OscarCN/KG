# TODO — Extraction LLM swaps day↔month when emitting ISO dates

**Status:** open — diagnosed, fix not started
**Area:** `src/entities/extraction/` (prompts + optional post-parse guard); surfaced by `scripts/reconcile_dryrun.py`
**Related:** [`canonical_reconciliation.md`](canonical_reconciliation.md), [`../linking.md`](../linking.md)

## Problem

The extraction LLM sometimes writes the **ISO `date_range.start`/`end` with day and
month transposed**, even though the `mention` it quotes carries the correct Spanish
date. Confirmed against the raw LLM cache (not the schema parser):

| entity | `mention` (correct) | LLM `start` (wrong) |
|---|---|---|
| 274 | `11 de junio de 2026 a las 13:00 horas` | `2026-11-06 13:00:00-06:00` |
| 111 | `del 11 de junio al 19 de julio` | `2026-11-06 00:00:00-06:00` |

`11 de junio` (June 11) becomes `2026-11-06` (Nov 6): `11`→month, `06`→day, time
preserved. The raw extraction JSON in `cache/` already carries the swapped ISO, so the
model produced it — **not** the parser.

## Not the parser

`src/schema/types/dates.py` is correct: `_ISO_DATE_RE` keeps ISO (`YYYY-MM-DD`) strings
month-first and applies `dayfirst=True` only to human `DD/MM` strings. Fed the *mention*
`"11 de junio de 2026"`, it returns June 11. It's fed the already-swapped ISO, so it
faithfully stores Nov 6. This is distinct from the historical `schema_etl` `dayfirst`
parser bug (that path isn't involved in kg extraction).

## Impact

- Forks canonical twins offset by the swap (a June-11 event and its Nov-6 shadow) — e.g.
  the `2026-11-06` shadows of `México vs Sudáfrica`, `Zona Fest`/party, `MéxicoQ Zona Fest`
  at Estadio Corregidora.
- The name-led [reconciliation](canonical_reconciliation.md) pass *heals* these because
  the swapped dates are imprecise (`precision_days=null`), so its precision-aware date
  gate never rejects on them — but the underlying data is still wrong, and any date-precise
  consumer (reports, timelines) sees the wrong month.

## Fix directions

1. **Prompt** — tighten the `DateRangeFromUnstructured` date instructions in the generated
   prompts: emit ISO 8601 `YYYY-MM-DD`, and add an explicit Spanish-month→number worked
   example (`"11 de junio" → 2026-06-11`). `{date_now}` is currently rendered `dd/mm/YYYY`,
   which may prime the swap; consider ISO there too.
2. **Deterministic mention→ISO reconciliation guard** (robust, model-independent) — when the
   `mention` contains an explicit parseable Spanish date, parse it deterministically
   (month-name map / `dateutil` dayfirst) and **correct** the emitted ISO when they disagree
   on day/month. Catches the swap regardless of prompt wording. Best as a post-extraction
   validator on `DateRangeFromUnstructured`.

## Validation

- Re-extract the affected articles and confirm `start`/`end` match the `mention` month/day.
- Census: count `entities` whose `date_range.date_range.start` month disagrees with an
  explicit month name in `date_range.mention` (a cheap SQL/py scan) before and after.
