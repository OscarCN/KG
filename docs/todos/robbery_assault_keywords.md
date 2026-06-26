# TODO — Tighten `robbery_assault_event` matching keywords

**Status:** open
**Area:** `src/entities/extraction/catalogues/keywords.xlsx` (and `event_types.csv`)
**Related:** [`extraction.md`](../extraction.md), [`retrieval_linking_per_supertype.md`](retrieval_linking_per_supertype.md)

## Problem

`robbery_assault_event` is meant to capture **discrete** robbery / assault /
property-or-person crime **incidents** — each one "a specific occurrence
distinguishable by its location and date — *not* a crime trend, security
overview, or statistics report" (schema `meta.description`). Its matching rules
in `keywords.xlsx` pull in much more than that:

| Class | `kw` / category | Issue |
|---|---|---|
| `robbery` | `"robo"`, `"robar"` | broad — matches `robo de identidad`, metaphorical uses |
| `assault` | `"asalto"`, `"asaltar"`, `"atraco"` | acceptable |
| `kidnapping` | `"secuestro"`, `"plagio"`, `"levanton"`, … | acceptable |
| `security_event` | `"seguridad"`, `"inseguridad"`, `"vigilancia"`, `"delincuencia"` | **thematic**, not incidents |
| `security_event` | bare `Seguridad` ES category | pulls **everything** tagged Seguridad |

The `security_event` rows are the main offender. `"seguridad"` matches
security-**policy** announcements, `"vigilancia"` matches surveillance-camera
installations, `"delincuencia"` matches crime-**trend** op-eds and statistics —
exactly the content the schema says to reject. The bare `Seguridad` category
row widens this to every article ES tagged under security. Two consequences:

1. **Partition pollution.** The linker partitions on
   `(event_type, state, day)`. A `security_event` partition fills with
   non-incidents (policy, trends, overviews) that have no clean location/date
   identity, inflating the candidate set the LLM adjudicator must sift and
   adding cost with no merge signal.
2. **Ontology overlap.** `security_event` as an *event* class largely
   duplicates the `security` **theme** supertype (`crime_trends`,
   `law_enforcement`, `public_safety`, `security_policy`). Thematic security
   discourse belongs on the theme, not on a geo-event class.

Downstream LLM classification *does* filter some of this out (the schema
description instructs it to reject trends/overviews), so the end-to-end damage
is partly absorbed — but the over-matching still inflates extraction cost and
the linking partition before that filter runs.

## Goal

Make `robbery_assault_event`'s keyword rules match **discrete incidents**, so
its linking partition contains linkable occurrences rather than thematic
security content.

## Candidate changes (decide during implementation)

- **Drop or narrow the `security_event` keyword row** — remove the bare
  thematic terms (`seguridad`, `inseguridad`, `vigilancia`, `delincuencia`); if
  a catch-all is still wanted, gate it behind incident-bearing phrases.
- **Remove the bare `Seguridad` category row** for `security_event` (row 20) —
  it routes the entire security category into the event class.
- **Reconsider the `security_event` event_type itself** — likely retire it in
  favour of (a) the concrete `robbery` / `assault` / `kidnapping` classes for
  incidents and (b) the `security` theme for thematic coverage. If kept, scope
  it to incidents that don't fit the other three.
- **Add `not` phrases** to exclude trend/policy language
  (`"tendencia"`, `"estadisticas"`, `"politica de seguridad"`, `"operativo"` as
  appropriate) where a keyword must stay broad.

## Acceptance

- A sample pull on the revised rules yields predominantly discrete crime
  incidents (manual spot-check), not security policy / trend / statistics
  articles.
- The `(security_event, state, day)` linking partition no longer fills with
  non-incident records on a large-scale run.
- `event_types.csv` and `keywords.xlsx` stay consistent; extraction docs
  updated if the `security_event` class is retired or rescoped.
