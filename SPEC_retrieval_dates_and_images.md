# Spec — real event dates for retrieval + doc_images back in production

Session scope: the `kg` repo (+ the kgdb schema owned by `media-backend-paid`).
Requested by the DeepRiver consumer platform (`media/cc`), which reads kgdb through
its read API. Two independent workstreams; ship separately.

Context recap (verified live, 2026-08-06, against stg kgdb via the `cc` read API):

- `event_properties.date_start/date_end` carry a **±slack linking window** (by
  design — see `src/entities/linking/persistence.py` docstring item 6 and the
  `slack_days` handling around line 184). They are the ONLY queryable date
  columns, so every reader (the `cc` read API, and by extension its map, "HOY"
  filter, "EN CURSO · HASTA …" labels) shows the padded window as if it were the
  event's real dates. Example: event 483, extraction says start `2026-08-06T10:00`
  end `null` (`metadata.date_range`, `precision_days: 0`), columns say Aug 5 → Aug 7.
  303 of 676 events with a `date_end` have exactly a 2-day span — the ±1 stamp.
- `entities_documents.doc_images` is empty in **all 73,779 rows** of live stg
  kgdb, so every consumer gets `imageUrl: null` and the Ambiente poster wall
  (image-forward by invariant) renders empty. The write path exists
  (`persistence.py` ~line 269: upsert with `COALESCE`, `NULL` = "not captured
  (old records / no ledger)") and so does `scripts/backfill_doc_images.py` —
  the feature was previously live-verified (198/200 highlights with hero, per
  `cc/docs/status/geo-events.md`), so this is a deploy/pipeline regression, not
  missing code.

---

## Workstream A — real event dates: `event_date_start` / `event_date_end`

**Goal:** retrieval gets the extraction's actual dates in queryable columns; the
slack window stays untouched for linking.

1. **Schema (schema-first rule — SQL lives in `media-backend-paid`):**
   - Edit `media-backend-paid/db/kg_db/schema.sql`: add to `event_properties`
     ```sql
     event_date_start timestamptz,   -- extraction's actual start (metadata date_range)
     event_date_end   timestamptz    -- NULL = punctual / no known end (do NOT synthesize)
     ```
   - New migration file in `media-backend-paid/docs/db/migrations/` with the
     ALTER + one-time backfill from the blob the extraction already writes:
     ```sql
     UPDATE event_properties SET
       event_date_start = (metadata #>> '{date_range,date_range,start}')::timestamptz,
       event_date_end   = (metadata #>> '{date_range,date_range,end}')::timestamptz
     WHERE metadata ? 'date_range';
     CREATE INDEX ON event_properties (event_date_start);
     ```
     Check the actual blob shape on a few rows first (nesting is
     `metadata -> date_range -> date_range -> start/end`); handle rows where the
     inner value is missing/empty string without failing the migration.
   - Fold into `schema.sql` once applied (workspace convention).

2. **Writer (`src/entities/linking/persistence.py`, the INSERT around line 238):**
   write both pairs on insert AND in the `ON CONFLICT` update:
   `date_start/date_end` = slack-widened (unchanged), `event_date_start/event_date_end`
   = the extraction's dates verbatim (end stays NULL when unknown — never mirror
   start into end, never apply slack). Update `test_persistence.py` accordingly.

3. **Docs:** the kg→kgdb persistence contract lives in
   `media-backend-paid/docs/DATABASE_POSTGRES.md` under *KG entity extraction &
   linking integration* — document the two-pair semantics there (linking window
   vs. real dates), so no future reader "fixes" the slack again.

4. **Verify:** after deploy + migration, for a handful of events compare
   `event_date_*` against `metadata.date_range` (equal), and confirm event 483
   reads start Aug 6 / end NULL. Downstream (`cc` backend) will switch its
   SELECTs to `COALESCE(event_date_start, date_start)` — not this session's job,
   but the columns must exist and backfill must have run before it can.

## Workstream B — `doc_images` empty in production

**Goal:** figure out why the deployed pipeline stopped writing `doc_images`,
fix it, and backfill.

1. **Diagnose first, don't assume:** the upsert comment says NULL = "no ledger".
   Candidates, in order: (a) the deployed kg worker predates the doc_images
   feature (check deployed image/tag vs. the commit that added it), (b) the
   image ledger the persistence reads is empty/unreachable in prod (find what
   feeds it — trace where `doc_images` values come from before the upsert),
   (c) the upsert `COALESCE` never overwrites, so early NULL rows stay NULL even
   after a fix — meaning a backfill is REQUIRED, not optional.
2. **Fix** whichever it is; redeploy the worker.
3. **Backfill:** `scripts/backfill_doc_images.py` exists — read it, confirm it
   still matches the current schema/ledger, run it for at least the last ~60
   days of docs (the consumer surfaces only look ahead/recent).
4. **Verify end-to-end:** in kgdb,
   `SELECT count(*) FROM entities_documents WHERE doc_images IS NOT NULL AND doc_images <> '[]'`
   goes from 0 to substantial; then from the `cc` side,
   `GET /events/highlights?lens=ambiente&…` returns `imageUrl != null` for most
   rows (was 0/8; historical healthy mark 198/200).

---

## Rules / cautions

- kgdb is shared ground truth: schema changes only via the SQL files +
  migrations in `media-backend-paid` (never declare columns in Python).
- Do NOT touch the slack logic or existing `date_start/date_end` semantics —
  entity/event linking depends on the widened window.
- `event_date_end` NULL is meaningful (punctual event). Consumers render "no
  end"; don't invent one.
- Staging/production discipline: these changes target the live stg kgdb that
  production `kg` writes to — coordinate the migration with the running worker
  (additive columns are safe to apply before the writer deploys).
