# Geocode with document context (rescue no-anchor events)

## Problem

~96 kg events carry a venue/street but **no state or city** in the extracted
`location` (plus `_geo` absent entirely for ~55): `Campo Marte`, `Pirámide de la
Luna`, `Plaza de cobro Chamapa`, `Banco del Bienestar`, `Autopista Durango-Mazatlán`…
With country-only context the geocoder strips the lower-level mentions and returns L1
or nothing. The information usually exists **in the document** — other location
mentions in the article, or the source's own geography — just not in the extracted
record's `location` dict.

## Direction

The geocoder API already supports a second **context group** (`context_group: 2` in the
mention list; the GET path takes a `context` text): context mentions disambiguate
without being part of the match. Options, cheapest first:

1. **Sibling-record context**: other events/records extracted from the *same document*
   often carry the missing state/city — pass their location fields as context-group-2
   mentions when the target record lacks an anchor.
2. **Source metadata**: the outlet's own location (Mongo source store,
   `admin_app.CrawlersAll` — the coverage-signal data cc already uses) as a weak
   state-level context mention.
3. **Full-document NER**: send article text through the tagger (`GET /geocoder`
   path) and merge its location entities as context. Highest recall, costs an NER call.

Overlaps with the grounding line's Phase-1 geo enrichment
(`kg/grounding/docs/roadmap.md` §Phase 1) — where a document gets stamped with resolved
locations once, this TODO becomes "reuse that stamp as geocode context".

Defer until the cheap cleanup sets are exhausted (see the geocoding repo's
`docs/todos/cleanup_next_pass.md`); measure on the ~96-event backlog when picked up.
