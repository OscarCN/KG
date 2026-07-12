# Skip geocoding foreign-location events (mark out-of-scope)

## Problem

Extraction regularly yields events located outside Mexico (concert/tour coverage:
Movistar Arena Buenos Aires/Santiago/Bogotá, Hammerstein Ballroom NY, Hollywood…).
The linker geocodes them anyway; the Mexico-scoped geocoder returns a coarse
country/state match or nothing, and the events land in kgdb looking like *badly
geocoded Mexican events* (~26 venue-carrying events at precision ≤2 as of 2026-07-11).
They pollute every precision audit and cleanup worklist (see
`geocoding/docs/geocoding_data_cleanup.md` in the geocoding repo — the recurring
cleanup process has to re-classify and skip them each run).

## Fix

In `src/entities/linking/geocode.py` (or the strategy's enrich step): when the
extracted `location.country` is present and not Mexico (`mexico/méxico/mx`), **skip the
geocoder call** and stamp the record `_geo_source: "out_of_scope"` (no `_geo`). The
geocoder's own `OUT_OF_SCOPE` enrichment reason is the server-side mirror of the same
rule; deciding client-side saves the call entirely.

Persistence: events keep `entities.metadata.location` (the extraction is still true);
they just carry no Mexican geo row, so map/product queries and precision audits exclude
them naturally.

Decide: also apply to `state` values that are known foreign states when `country` is
missing (Florida, Cundinamarca, Buenos Aires…) — a small static list catches most.

~5 lines + a test. No backfill needed beyond optionally clearing the existing ~26
events' `_geo`.
