# Deployment tracker — CDMX-lluvias work → production

Single tracker for everything pending to deploy the work of 2026-08-07/08 (flood
ontology widening, author-context geocoding, the Ciudad situation PoC) to prod.
Broader go-live context: [productionization_streaming_kg.md](productionization_streaming_kg.md)
(the standing checklist — k8s deploy, observability, cost controls all still open there).

## kg (this repo)

- [ ] **Commit the author-context chain** (uncommitted): `src/entities/document.py`
  (`location_author` passthrough), `src/entities/extraction/extract.py`
  (`_author_geo` provenance backfill), `src/entities/linking/geocode.py`
  (context-group-2 mentions + bare fallback + `_author_context_used`),
  `src/entities/linking/strategy.py` (wire-through), `src/PoC/get_data.py`
  (NEWS_FIELDS), `docs/linking.md` (docs). Design/evidence: geocoding repo
  `docs/todos/kg_social_cdmx_lluvias_geo_review.md` §3.4.
- [ ] **Restart the listener on the new code.** The running bare-metal listener
  (run tag `cdmx-lluvias-2026-08-07`) predates the author-context change; restart
  also picks up any live-kgdb ontology rule edits (rules load once at startup — no
  hot reload). ⚠️ The listener (and the cc dev servers below) were started as
  session-bound background tasks — **they stop when the Claude session ends**; the
  queue is durable, so messages just accumulate until relaunched:
  ```sh
  cd ~/ocn/media/kg/kg && set -a && source .env.stg && set +a && \
    python3 -u src/listener.py >> data/.runlogs/listener_$(date +%F).log 2>&1 &
  # cc dev stand-ins (until the docker image is rebuilt):
  cd ~/ocn/media/cc/dr_backend && source setup_local.sh && \
    KG_DB_HOST=localhost REDIS_HOST=localhost ELASTIC_HOST=localhost MONGO_HOST=localhost \
    ELASTIC_AUTH=$(grep ^ELASTIC_AUTH= .env | cut -d= -f2 | awk '{print $1}') \
    ELASTIC_HTTP_CERT=$(grep ^ELASTIC_HTTP_CERT= .env | cut -d= -f2 | awk '{print $1}') \
    .venv/bin/uvicorn app.main:app --port 8010 &
  cd ~/ocn/media/cc/dr_frontend && NEXT_PUBLIC_API_BASE_URL=http://localhost:8010 npx next dev -p 3000 &
  ```
  (Exporting `ELASTIC_AUTH`/`ELASTIC_HTTP_CERT` is required — the shared
  `elastic_client` reads process env, not `.env`; missing them = the
  CERTIFICATE_VERIFY_FAILED home breakage.)
- [ ] **Prod env hygiene.** `.env.stg`/`.env.local` were flipped to `localhost`
  (SSH-tunnel hosts) during the network outage — revert to the direct
  `192.168.1.x` hosts (or keep the tunnel convention deliberately) before any
  non-tunnel deployment. Keep: `FILTER_GEO=_48409,_48422,_48402` (all-CDMX scope)
  and `KG_ONTOLOGY_SOURCE=db`. Pick a fresh dated `KG_RUN_TAG` per bounded run.
- [ ] **Run tag strategy**: switch to a stable/periodic tag once consumption is
  continuous (per productionization Phase 1 provenance scheme).
- [ ] Optional: re-enqueue the one dead-lettered doc from the run (extraction JSON
  truncation) via `scripts/enqueue_from_es.py`.

## kg — 2026-08-09 social-matching fixes (uncommitted)

Root-caused why yesterday's 260-post CDMX social batch produced only 14 events
(see session notes: 121 dropped `out_of_scope` on bare precision-2 "CDMX"
mentions, 24 `no_match` on tokenization/keyword gaps):

- [x] **Matcher tokenization fix committed** (`src/entities/extraction/extract.py`):
  camelCase split before lowercasing (hashtag compounds → words) and
  alphanumeric-run tokenization for `kw` matching (punctuation no longer glues
  to tokens — `#lluvias`, `congreso".` now match). Docs: `docs/extraction.md`.
- [x] **Geo-scope city-state exemption committed** (`src/geo_scope.py` +
  `FILTER_GEO_CITY_STATES=_48409` in `.env.stg`): bare state-level CDMX
  mentions (precision 2) count as in scope — social posts usually carry only
  the bare "CDMX" mention (the Bunbury miss).
- [x] **Keyword additions committed** (`keywords.xlsx`, applied to live kgdb
  rules 1/39 in place): `concert` += auditorio, gira; `flood` += tormenta +
  phrases "alerta purpura/roja/amarilla", "corriente(s) de agua".
- [x] **`refine_mentions: ["CALLE"]` client hunk committed** (`geocode.py` — was
  a leftover uncommitted change from the 08-08 cycle). Live-tested: the
  geocoder returns a **single best match per context group**, so the
  multi-street half of [location_level_list_extraction.md](location_level_list_extraction.md)
  is blocked on a geocoder-side change (ask filed in the geocoding repo).
- [ ] `scripts/seed_ontology_rules.py` full refresh fails as `backend` user
  (`TRUNCATE … RESTART IDENTITY` needs the sequence owner) — reseed as owner or
  grant, next time the xlsx is the source of a bulk change.
- [ ] Listener restarted 2026-08-09 on the new code/rules (run tag
  `cdmx-lluvias-2026-08-09`, session-bound background task — same caveat as
  above). New TODO filed: [missing_person_amber_class.md](missing_person_amber_class.md).

## gp3

- [ ] **Redeploy with the widened kg whitelist**: `KgStreamPipeline.KG_DOC_FIELDS`
  += `location_author` (uncommitted in `~/ocn/media/gp3`). Until deployed, the kg
  listener receives no author geo and the context path is a silent no-op.

## apify_client

- [ ] **Next lluvia run with author enrichment** (`run_lluvia_cdmx.py` v2,
  `enrich_followers: true`, no likes gate): first run warms the UsersManagement
  profile cache (~$0.01/author); FB `location_author` only populates from the run
  AFTER the warm-up (pipeline stage ordering). X authors flow immediately.
- [ ] `.env` was flipped to `localhost` tunnel hosts — same revert-or-keep decision
  as kg's env files.

## Geocoder (geocoding repo — tracked there, listed for visibility)

Owned by `~/ocn/geocoding/docs/todos/kg_social_cdmx_lluvias_geo_review.md`:
- [ ] KB repairs for the batch (importance bumps: Periférico/Circuito Interior/
  Calz. Tlalpan…; missing places: Plaza Artz, estación Huipulco, Av. Vaqueritos;
  wrong-venue fixes) + kgdb `entity_locations` repair + batch replay.
- [ ] **Multi-context CER training samples (required)** — the author-context change
  makes context-group-2 input routine; mint the `author_context` embedded bucket,
  retrain, re-run the behavioral test (spec §3.5).
- [ ] After any KB fix: clear kg `cache/geocode/` + mind the geocoder's 24 h Redis
  call cache before replay/validation.

## cc (Ciudad situation PoC → ship decision)

All uncommitted in `~/ocn/media/cc`; running locally as dev stand-ins
(uvicorn :8010 + `next dev` :3000) that must be retired on ship:
- [ ] Review + commit: backend (`app/services/situation.py`, `situation_rows` in
  `app/clients/kgdb.py`, models, router, config knobs) and frontend
  (`CiudadSituation.tsx`, types, api client, `app/ciudad/page.tsx`).
- [ ] Pre-ship fixes agreed: document the endpoint contract in
  `docs/endpoints/map.md`; tests; `docs/status/geo-events` note. Known limits to
  either accept or fix: absolute thresholds (vs 7-day baseline), single leading
  category (compound situations show as "colaterales"), image-poor highlights.
- [ ] **Rebuild the `dr_backend:dev` Docker image** (it bakes code — no mount) and
  restart the container so :8000 serves `/ciudad/situation`; then point the
  frontend back at :8000 and kill the :8010 uvicorn + the replacement `next dev`.
- [ ] Note for local dev: never run two `next dev` servers off one working tree
  (shared `.next` inlines `NEXT_PUBLIC_*` across both — the 2026-08-08 home
  breakage).

## Done this cycle (for the record)

- Flood ontology widening committed (kg `9fdac20`) + live kgdb
  `ontology_matching_rules` row 39 updated in place.
- Live kgdb Phase-1 state unchanged; canary run tag `cdmx-lluvias-2026-08-07`
  produced the CDMX flood ground truth (29+ flood canonicals, 45+ sources).
- gp3 `KG_QUEUE` firehose confirmed live; consumer-side `FILTER_GEO` widened to
  all-CDMX.
