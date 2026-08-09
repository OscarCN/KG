# Extraction: "Pueblo X" → `neighborhood`, "alcaldía X" → `city` (CDMX)

## Problem

The extractor **drops** colonia- and alcaldía-level qualifiers instead of
leveling them. Evidenced by event 324
(`20260805_ciudad-de-mexico_e93f3cc1e5b74c76bd369492`, "Mayordomía de San
Ignacio de Loyola"). The article read:

> Lugar: Francisco I Madero No 2, **Pueblo Santa Martha Acatitla**,
> **alcaldía Iztapalapa**.

Extracted location:

```json
{"country": "Mexico", "state": "Ciudad de Mexico", "city": "Ciudad de Mexico",
 "neighborhood": null, "street": "Francisco I Madero", "number": "2"}
```

Both qualifiers vanished: `neighborhood` stayed null and `city` got the generic
"Ciudad de Mexico" instead of the alcaldía. The geocoder was left to pick among
the dozens of CDMX streets named "Francisco I. Madero" with a city-wide anchor
and landed in **Coyoacán** (colonia Viejo Ejido Santa Úrsula Coapa) — wrong
alcaldía entirely. With the full extraction the same geocoder resolves
decisively (CER 1.128 vs 0.15) to `_484090070001010300119` — FRANCISCO I.
MADERO under **PUEBLO SANTA MARTHA ACATITLA**, Iztapalapa. The KB needed
nothing; the extraction was the only gap. (Event 324 was hand-repaired in kgdb
on 2026-08-06 — `entity_locations` + `metadata._geo` + `metadata.location`;
pre-snapshot in
`geocoding/data/kb_mutation_ledgers/2026-08-06_event324_madero_repair_presnapshot.jsonl`.)

## Spec (extraction prompts / Location schema guidance)

- **"Pueblo X" inside an urban municipality / CDMX alcaldía → `neighborhood`.**
  CDMX *pueblos originarios* (Pueblo Santa Martha Acatitla, Pueblo Santa Úrsula
  Coapa, San Andrés Mixquic, …) are colonia-level entities — INEGI models them
  as colonias and the geocoding KB carries them at level 5 (e.g. PUEBLO SANTA
  MARTHA ACATITLA = `_4840900700010103`). Keep the "Pueblo" prefix in the
  value — the KB names carry it.
  ⚠️ This REFINES [`location_leveling_extraction.md`](location_leveling_extraction.md),
  which says settlements (poblado/comunidad/ejido/pueblo) belong in `city`:
  that rule is for *rural* standalone localities. The discriminator is
  context: a pueblo **qualified by an alcaldía/city in the same address** is a
  neighborhood; a pueblo standing alone as the settlement is a city/locality.
- **"alcaldía X" / "delegación X" → `city`** — never dropped, never left as the
  generic "Ciudad de México" when a specific alcaldía is named (the 16
  alcaldías are the municipality level of CDMX; the geocoder partitions on
  `level_3_id`). "Ciudad de México" belongs in `state`.
- **Barrio / Barrio de X** — same treatment as Pueblo (colonia level →
  `neighborhood`).
- Few-shot the exact event-324 address in the prompt: it exercises all three
  slots at once (`street` + `number`, pueblo → `neighborhood`, alcaldía →
  `city`).

## Why it matters

A dropped colonia doesn't just cost precision — it turns street homonyms into
coin tosses (Madero, Juárez, Hidalgo, Zaragoza exist in nearly every alcaldía),
and the wrong pick then poisons the geo partition (`level_3_id` Coyoacán vs
Iztapalapa ⇒ the hard geo gate keeps true co-events apart *and* invites merges
with unrelated Coyoacán events). The geocoder-side homonym weakness is known
and tracked (geocoding repo, corridor-homonym bucket); feeding it the anchors
the article already contains is the cheap, correct fix.

## Backlog sweep (downstream half)

Events whose extracted `location` has `street` set, `neighborhood` null, and a
CDMX/urban `city`, where the source text contains `pueblo|barrio|alcaldía|
delegación` near the address: re-extract (or hand-level) and re-geocode;
upgrade under the runbook name-gate + containment rules
(`geocoding/docs/geocoding_data_cleanup.md` §5–6). Event 324 is done.
