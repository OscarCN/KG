# Extraction: standardize extracted-date timezones (Mexico City, not machine-local)

## Problem

Extracted `date_range` datetimes reach kgdb with the wrong (or accidental) UTC offset. Live
example — event **483** (`protest_event`, mention `"10:00 horas"`, article published
2026-08-06 06:48 CST):

```json
{
  "date_range": { "start": "2026-08-06T10:00:00+00:00", "end": null },
  "timezone": null,
  "mention": "10:00 horas",
  "precision_days": 0
}
```

"10:00 horas" in a Mexican news article means 10:00 **local** (`America/Mexico_City`,
UTC-6), but the stored instant is 10:00 **UTC** = 04:00 CST — off by six hours.

## Root cause

Two compounding gaps in the schema type layer:

1. **The naive-datetime default is machine-local, not Mexico City.**
   `src/schema/types/base.py` sets `local_tz = datetime.now().astimezone().tzinfo` and
   `DateTimeParser` (`src/schema/types/dates.py`) attaches it only when the parsed value is
   naive. On a laptop in CDMX that happens to be right; in the **Dockerized streaming
   listener the container clock is UTC**, so every naive extracted datetime gets stamped
   `+00:00`. The repo convention ("datetimes default to Mexico City timezone") is only true
   by accident of where the code runs.
2. **The extracted `timezone` field is never applied.** `DateRangeFromUnstructured` carries
   a `timezone` subfield, but nothing consults it — whether the LLM fills it or leaves it
   null, the offset comes from the parser default (or from an explicit offset the LLM chose
   to emit, e.g. a trailing `Z`, which is kept as-is even when wrong).

## Impact

- Every stored instant can be off by 6 hours (`+00:00` vs CST). Harmless for day-granularity
  linking *most* of the time (±1-day slack absorbs it), but an event mentioned near midnight
  shifts to the **wrong day**, which corrupts the candidate index day-keys, the deterministic
  gate's `det_day_slack` comparison, and the `event_properties` confidence window.
- Downstream consumers (reports, dashboards, tags) render the wrong local time.
- Mixed-offset records make window-width / precision comparisons subtly inconsistent.

## Status: fix implemented (2026-08-06); metadata repair of old rows remains

1. ~~**Pin the default**~~ **Done** — `local_tz = ZoneInfo("America/Mexico_City")`
   (`src/schema/types/base.py`); `tzdata` added to `requirements.txt` so it resolves on the
   Alpine listener image.
2. ~~**Honor the extracted `timezone` field**~~ / 3. ~~**Distrust bare LLM offsets**~~
   **Done** — `_normalize_date_timezones` (`src/entities/extraction/extract.py`), applied to
   every record in `_validate_all_entities`: naive **and UTC-stamped (offset 0)** datetimes
   are re-anchored (wall clock kept) to the record's `timezone` when valid, else the pinned
   default; genuine non-zero offsets are kept; the applied zone is stamped back into
   `timezone`. Covered by `src/entities/extraction/test_normalize_dates.py`.
4. **Backfill/repair — partially done, partially open.**
   - `event_properties.event_date_start/event_date_end` (the new user-facing columns) were
     backfilled with wall-clock re-anchoring to CDMX by
     `media-backend-paid/docs/db/migrations/event_dates_user_facing_kgdb.sql`.
   - **Open:** `entities.metadata.date_range` (and the retrieval window
     `event_properties.date_start/date_end`) of rows written by the UTC container still carry
     `+00:00` stamps. Deliberately deferred: linking is self-consistent (windows are compared
     against each other at day granularity) and user-facing reads now come from the corrected
     `event_date_*` columns. Repair alongside a future metadata pass if needed.

Prompt-side (`{date_now}` anchoring, emitting `timezone`) can still help but the deterministic
parser-side pin is the real fix — same philosophy as the mention→ISO guard in
[extraction_date_daymonth_swap.md](extraction_date_daymonth_swap.md).
