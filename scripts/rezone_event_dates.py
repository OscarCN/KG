"""One-off re-mint of UTC-anchored ``event_properties.event_date_*`` to CDMX.

Date-only events carry no time of day, so the linker stores them as MIDNIGHT.
Which midnight is the question: ``_normalize_date_timezones`` anchors extracted
datetimes to ``America/Mexico_City`` (``_DEFAULT_TZ``), and even re-anchors an
explicit ``Z`` because "the extraction LLM never legitimately means UTC for
Mexican news". So a date-only event on Aug 9 is stored as ``2026-08-09T06:00Z``
(= midnight CDMX, since CDMX is UTC-6) and its end as ``2026-08-10T05:59:59Z``
(= 23:59:59 CDMX the same day).

A residue predates that normalizer and sits at midnight UTC instead:

    start 2026-08-09T00:00:00Z   (should be 06:00:00Z)
    end   2026-08-09T23:59:59Z   (should be 2026-08-10T05:59:59Z)

Those rows keep re-appearing in fresh writes because the extraction cache
replays the old record verbatim on re-link — measured 2026-08-11: rows written
in the last 4 days split 920 CDMX-anchored / 60 UTC-anchored, and the UTC group
has a markedly lower median entity_id (983 vs 1683), i.e. they are OLD entities
being re-persisted, not new mistakes.

Why it matters downstream: readers convert these instants to the explorer's
zone (CDMX). A CDMX-anchored midnight converts back to the intended day; a
UTC-anchored one converts to 18:00 the PREVIOUS day, moving the event. With
both conventions live, no reader can be correct for both — which is exactly why
this backfill has to land before the read side switches to civil-day
conversion.

SCOPE — only the user-facing ``event_date_start`` / ``event_date_end`` are
touched. ``date_start`` / ``date_end`` are the linker's slack-widened retrieval
window and are none of this script's business.

Only exact midnight-UTC markers are rewritten (``00:00:00`` start, and an end at
``00:00:00`` or ``23:59:59``). A row whose start carries a real time of day is
left alone: an event genuinely starting at 00:00:00Z is indistinguishable from a
date-only marker, and the marker reading is right for ~300 rows while the other
is right for a handful.

Every changed row's OLD values are written to a JSON backup before the UPDATE,
so a bad run can be reversed with ``--revert <backup.json>``.

Env (kg/.env.local): KGDB_HOST/PORT/USER/PASSWORD/NAME.

Usage:
    python scripts/rezone_event_dates.py --dry-run
    python scripts/rezone_event_dates.py --apply [--limit N]
    python scripts/rezone_event_dates.py --revert backups/rezone_<stamp>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
load_dotenv(_PROJECT_ROOT / ".env.local")

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

CITY = ZoneInfo("America/Mexico_City")
UTC = timezone.utc


def _kgdb():
    return psycopg2.connect(
        host=os.environ["KGDB_HOST"],
        port=int(os.environ.get("KGDB_PORT", 5432)),
        user=os.environ["KGDB_USER"],
        password=os.environ["KGDB_PASSWORD"],
        dbname=os.environ["KGDB_NAME"],
    )


# Rows whose start is exactly midnight UTC — the pre-normalizer marker.
_SELECT = """
    SELECT event_id, event_date_start, event_date_end
      FROM event_properties
     WHERE event_date_start IS NOT NULL
       AND (event_date_start AT TIME ZONE 'UTC')::time = TIME '00:00:00'
     ORDER BY event_id
"""


def _remint(start: datetime, end: datetime | None) -> tuple[datetime, datetime | None]:
    """Re-anchor a UTC-midnight day marker to the same CALENDAR DAY in CDMX.

    The written date is the day the extraction asserted, under either anchor —
    that is what makes this safe. Only the anchor moves; the day never does.
    """
    day = start.astimezone(UTC).date()
    new_start = datetime(day.year, day.month, day.day, tzinfo=CITY).astimezone(UTC)
    if end is None:
        return new_start, None
    e_utc = end.astimezone(UTC)
    if (e_utc.hour, e_utc.minute, e_utc.second) == (23, 59, 59):
        # inclusive end-of-day marker -> 23:59:59 CDMX of ITS OWN written day
        eday = e_utc.date()
        new_end = datetime(eday.year, eday.month, eday.day, 23, 59, 59, tzinfo=CITY).astimezone(UTC)
    elif (e_utc.hour, e_utc.minute, e_utc.second) == (0, 0, 0):
        eday = e_utc.date()
        new_end = datetime(eday.year, eday.month, eday.day, tzinfo=CITY).astimezone(UTC)
    else:
        return new_start, end  # a real time of day — leave it exactly as it is
    return new_start, new_end


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="report what would change")
    g.add_argument("--apply", action="store_true", help="write the changes")
    g.add_argument("--revert", metavar="BACKUP", help="restore from a backup json")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    conn = _kgdb()
    conn.autocommit = False

    if args.revert:
        rows = json.loads(Path(args.revert).read_text())
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    "UPDATE event_properties SET event_date_start=%s, event_date_end=%s "
                    "WHERE event_id=%s",
                    (r["old_start"], r["old_end"], r["event_id"]),
                )
        conn.commit()
        print(f"reverted {len(rows)} rows from {args.revert}")
        return

    with conn.cursor() as cur:
        cur.execute(_SELECT)
        rows = cur.fetchall()
    if args.limit:
        rows = rows[: args.limit]

    changes = []
    for event_id, start, end in rows:
        new_start, new_end = _remint(start, end)
        if new_start == start and new_end == end:
            continue
        changes.append(
            {
                "event_id": event_id,
                "old_start": start.isoformat(),
                "old_end": end.isoformat() if end else None,
                "new_start": new_start.isoformat(),
                "new_end": new_end.isoformat() if new_end else None,
            }
        )

    print(f"candidates (start at 00:00:00Z): {len(rows)}")
    print(f"rows to change                 : {len(changes)}")
    for c in changes[:5]:
        print(f"  {c['event_id']:>6}  {c['old_start']} .. {c['old_end']}")
        print(f"          ->  {c['new_start']} .. {c['new_end']}")
    if len(changes) > 5:
        print(f"  … {len(changes) - 5} more")

    if args.dry_run or not changes:
        print("\ndry run — nothing written")
        return

    backup_dir = _PROJECT_ROOT / "backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"rezone_event_dates_{stamp}.json"
    backup.write_text(json.dumps(changes, indent=2))
    print(f"\nbackup written: {backup}")

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE event_properties SET event_date_start=%s, event_date_end=%s WHERE event_id=%s",
            [(c["new_start"], c["new_end"], c["event_id"]) for c in changes],
            page_size=200,
        )
    conn.commit()
    print(f"updated {len(changes)} rows")


if __name__ == "__main__":
    main()
