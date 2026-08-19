# Structured cartelera ingestion (venue/ticketing listings → kg queue)

## Problem

kgdb only knows events the ingested press corpus mentions, and mainstream press never
covers club-level programming. Live-DB evidence (2026-08-19 venue review): **Foro Indie
Rocks** — a venue with near-nightly gigs — has **2 events ever** in kgdb; Estadio GNP
Seguros has no events in the frontend's default cartelera window (hoy → +7) even though
44 GNP-venue events exist, all correctly geocoded at precision 7. The gap is **coverage,
not geocoding**: the sources that announce this programming (venue sites/socials,
ticketing platforms) don't flow into the doc queue at all.

## Decision

Ingest **structured cartelera sources** as a new source class — first-party/structured
event data alongside press and social — via a scraper that publishes **pre-extracted
records** to the existing kg queue. Chosen over the alternatives:

- *Not* pseudo-documents through the LLM path: pays extraction to recover structure the
  scraper already has, and re-introduces the date-precision noise structured data fixes.
- *Not* a direct kgdb writer: linking is the point — a Ticketmaster record for a concert
  must **merge with** the press-derived canonical, not sit beside it. Going through
  `link_one` gets dedup, the geo gate, and the merge for free.

## Design

- **Producer**: scraper publishing to the kg queue (MVP: a `scripts/`-level producer like
  `enqueue_from_es.py`; own repo later per workspace convention). Daily cron.
  - Phase 1 targets: **Ticketmaster Discovery API** (official, structured JSON, covers
    the big CDMX venues, includes poster images) + **one venue-site scraper: Foro Indie
    Rocks** (indierocks.mx cartelera) to prove the small-venue case.
  - Phase 2: Boletia / venue whitelist expansion (CDMX-first), cancellation/reschedule
    sync (platform status → `event_properties.status`).
- **Message shape**: a new envelope kind carrying an already-validated `paid_mass_event`
  record — name, child type (`concert`/`festival`/…), `date_range` with
  `precision_days: 0` and exact start/end, venue as `location.place_name`, price range,
  stable external event id.
- **Listener branch**: on the structured kind, **skip `Ontology.match` + LLM
  extraction**; still run the schema `Parser` (normalization), then the unchanged tail:
  geocode → `link_one` → `upsert_linked`. All three idempotency layers apply if the doc
  id is minted stably (`tm_<event_id>` + content hash, so a rescheduled event re-enters
  and merges rather than being Redis-deduped away).
- **Provenance & weighting**: records carry `_extraction_source: "structured"`. Exact
  windows already rank well under `_effective_precision_days`; the explicit rule to state
  at merge time: **a structured source's dates/venue win conflicts with press-derived
  ones** (interacts with the best-window bugs —
  [merge_narrow_window_overwrites_span.md](merge_narrow_window_overwrites_span.md),
  [merge_best_window_blocks_end_dates.md](merge_best_window_blocks_end_dates.md)).
  Structured records are the inverse of the pub-date-fallback failure (a dateless
  7-source Enjambre canonical stamped with its publication date): here dates are the
  *strongest* field, and can anchor/correct press-derived windows.

## Open questions

- **`entities_documents` for non-ES sources**: structured records have no ES doc. Mint
  synthetic doc rows (`doc_source` = platform, `doc_images` = poster art — a direct
  frontend win for the ambiente poster rail)? Or no doc row (then `source_count` and the
  source list undercount)? Leaning synthetic rows; needs a `news_type`/`doc_index`
  convention check against `media-backend-paid` consumers.
- Whether the structured envelope enters the same queue (listener branches on kind) or a
  dedicated queue with the same worker — same code either way; ops preference.
- Ticketmaster API terms/rate limits for this use; fallback is scraping the public
  listings.

## Status

Proposed (2026-08-19). Not started.
