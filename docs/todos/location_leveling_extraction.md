# Extraction leveling: localities/alcaldías end up in `place_name`

## Problem

The extractor fills `location.place_name` with names that are **not venues**:
localities/poblados (`San Pedro Escanela`, `Los Arredondo`, `Chichimequillas`,
`Calamanda`), alcaldías (`Iztapalapa`), and generic admin phrases
(`cabecera municipal`). Downstream, the linker geocodes them as level-7 (LUG) mentions:
they don't resolve (event stuck at L2), and the venue-creation pipeline must hold them
out by hand every cleanup run — minting them as venues would be poison (false
precision + generic names in the venue namespace; see the 2026-07-11 run,
`geocoding/docs/geocoding_data_cleanup.md` §1 in the geocoding repo).

~15 venue-carrying events at coarse precision trace to this as of 2026-07-11.

## Fix (upstream half)

In the Location schema guidance / extraction prompts: `place_name` is for **venues** —
named establishments, buildings, plazas, parks. Settlements (poblado, comunidad,
ejido, localidad, pueblo) belong in `city` (level 3/4); alcaldías/delegaciones in
`city`; "cabecera municipal" and similar descriptors should resolve to the
municipality, not a venue. Add few-shot negatives to the prompts (the examples above
are real).

## Fix (downstream half — already specified)

The geocoding repo's `docs/todos/cleanup_next_pass.md` §2 covers re-leveling the
*existing* mis-leveled events using Google's cached `types == ['locality','political']`
verdicts — no re-extraction needed for the backlog.

Related: [`location_level_list_extraction.md`](location_level_list_extraction.md)
(multi-location events — the other structural `location` extraction gap).
