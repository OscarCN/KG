# TODO — Best-window merge lets a precise start-only mention veto every later end date

**Status:** open — diagnosed 2026-08-11 (traced from DeepRiver consumer side), fix not started
**Area:** `src/entities/linking/strategy.py` (`_apply_best_window`), `src/entities/linking/aggregate.py`
**Related:** [`merge_narrow_window_overwrites_span.md`](merge_narrow_window_overwrites_span.md) (sibling failure mode of the same rule), [`extraction_precision_days_missing.md`](extraction_precision_days_missing.md), [`canonical_reconciliation.md`](canonical_reconciliation.md), [`../linking.md`](../linking.md)

## Problem

The question that surfaced this: *if a new article gives an end date to an open-ended
event (e.g. a multi-day street closure), does `event_properties.event_date_end` update?*

The plumbing says yes: on every re-mention the linker's `_update` path rewrites
`event_properties` via a true upsert (`persistence.py` `_write_event_properties`,
`ON CONFLICT … DO UPDATE SET event_date_end = EXCLUDED.event_date_end`). But whether the
merged record *carries* the new end is decided by the canonical-date policy
(`bounded_merge_widening = True` → `_apply_best_window`, `strategy.py:948`): the
canonical range is copied from the **single most precise extracted window** among the
accumulated `_source_windows` — precision = `precision_days` when present (authoritative),
else window width, with start-only-no-precision = ∞; ties keep the earliest-seen.

That conflates "most precise **start**" with "best **window**":

- Existing window start-only, `precision_days` null (effective ∞): a later end-bearing
  window (finite width) wins → end date lands. ✓
- Existing window start-only with **`precision_days: 0`** — the very common
  *"a partir de hoy a las 18:00"* extraction shape (live specimen: entity 2207) —
  effective precision 0 is **unbeatable**. A later *"el cierre durará hasta el viernes"*
  window (width ~4 days) loses the precision contest and its end date is **permanently
  ignored**. Another precision-0 window doesn't displace it either (earliest-seen tie). ✗

So exactly the events that most need converging end dates — open-ended closures,
plantones, works — are the ones whose ends can never arrive, because their first mention
tends to be a precise start-only announcement. (The legacy min/max widening would have
taken the end; the bounded policy was introduced for good reasons — outlier envelopes —
and shouldn't be reverted wholesale.)

Note the asymmetry with `status`: that column is last-writer-wins on every merge, so a
later article extracted as `past`/`completed` *can* end the event through the status
door. The end-date door is the one that's stuck.

## Consumer-side context (why this matters downstream)

DeepRiver (repo `cc`) now assumes, on both its map backend and frontend: a `planned`
event with **no end date is presumed not to outlast its own calendar day**, because
statuses rarely update. That day-ceiling is a workaround for precisely this gap — if
end dates converged as coverage accumulates, multi-day events would carry their real
windows and the heuristic would only cover genuinely unannounced durations.

## Direction

Pick **start and end independently** in `_apply_best_window`:

- `start` ← the most precise window (current rule, unchanged);
- `end` ← the most precise **end-bearing** window (when any exists), provided it
  coheres with the dominant cluster (`aggregate.dominant_window_cluster` already
  drops swapped/placeholder outliers);
- `precision_days` stays that of the start-winner (it describes the start's narrowness).

A start-only precision-0 mention remains the authority on *when it starts* while no
longer vetoing what later sources say about *when it ends*. Add a regression test in
`test_persistence.py`/strategy tests: precision-0 start-only window merged with a later
end-bearing window ⇒ canonical keeps the start, gains the end, and
`event_properties.event_date_end` updates on re-persist.
