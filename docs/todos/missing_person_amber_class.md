# New ontology class: missing person / Amber Alert

## Problem

Amber Alert posts are clean, structured, high-value events (name, age, last-seen
date, colonia + alcaldía) and we have no ontology class for them. In the
2026-08-08 CDMX social batch the same Iztapalapa Amber Alert appeared **3 times**
(Alerta Amber CDMX on FB and X, Diario CDMX on X), passed the geo scope
(precision-5 mention), and fell through matching as `no_match` — there is no
class whose keywords or semantics cover disappearances.

The nearest existing classes don't fit: `kidnapping` implies an abduction claim
the alert doesn't make; `security_event` is the thematic catch-all the
robbery/assault TODO is trying to *narrow*, not widen.

## Sketch

- New class `missing_person` (or `amber_alert`) under the security/incident
  supertype family (`security_event` supertype, or its own if the schema needs
  person-centric fields: name, age, last-seen date/location, alert status).
- Matching keywords/phrases: `"alerta amber"`, `desaparecido`/`desaparecida`,
  `"reporte de desaparicion"`, `localizar` (probably too broad alone — pair in
  phrases), `extraviado`/`extraviada`.
- Linking identity: person name + last-seen date — these events recur across
  sources verbatim (official alert text), so name-based dedup should be easy;
  note the linker currently only handles geo events, so this may land as an
  extraction-only (persist, no merge) class first, like themes.
- Wiring: `event_types.csv` row, entity schema (or reuse), extraction prompt,
  `keywords.xlsx` rule + kgdb `ontology_matching_rules` seed, kgdb type-catalog
  seed (P2).

## Evidence

2026-08-08 batch review (this repo, social CDMX run): 3 Amber posts missed;
see also the batch content review notes in the 2026-08-09 session (in-scope
no-event posts breakdown: 24 `no_match`, of which the Amber cluster was the
clearest new-type candidate alongside weather-alert keywords, since added to
`flood` rule 39).
