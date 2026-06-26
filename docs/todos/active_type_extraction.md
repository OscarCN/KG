# TODO — Classify & extract only *active* types (wire activation into the DB)

**Status:** open — design ready
**Area:** `src/entities/extraction/extract.py` (`Ontology`), `src/entities/extraction/catalogues/`,
kgdb catalog `entity_types_kinds_available`; couples to the catalog seed in
[`kgdb_event_persistence.md`](kgdb_event_persistence.md) (P2)
**Related:** [`kgdb_event_persistence.md`](kgdb_event_persistence.md),
[`../extraction.md`](../extraction.md)

## Goal

Classification and per-class extraction should consider **only active** ontology types.
Deactivating a supertype or a leaf type removes it from the `Ontology.match()` candidate set,
from the classification prompt, and from per-class extraction — **without deleting** its
matching rules or catalogue rows. The active/inactive decision becomes a **database** fact, so
it can be changed operationally (and, later, per-deployment) without editing the Excel
catalogue or redeploying.

## Today (what exists)

Activation already exists, but **only in Excel**: `catalogues/keywords.xlsx` has an `enabled`
column that gates each matching rule at load time — `enabled = FALSE` rows are skipped, so the
class is "never matched, classified or extracted" (missing column/value ⇒ TRUE, backward
compatible). See *Matching rules* in
[`extraction.md`](../extraction.md). This is
rule-level (a class with several rows can be partly disabled) and lives in a file, not the DB.

## Proposed: kgdb as the source of truth for activation

Wire activation into the kgdb type catalog `entity_types_kinds_available` (the table that
[`kgdb_event_persistence.md`](kgdb_event_persistence.md) P2 already seeds, one row per
supertype + one per leaf type):

- **Add `active boolean` to `entity_types_kinds_available`** (DB-authoritative). `keywords.xlsx`
  / `event_types.csv` stay the authoring source for keywords and class→supertype mapping; the
  DB owns *which* types are live.
- **Granularity: supertype *and* leaf type.** An inactive **supertype** disables all its leaves
  (it never reaches extraction). An active supertype with some inactive **leaves** narrows the
  classification candidates and per-class extraction. Activation is effectively
  `supertype.active AND leaf.active`.
- **`Ontology` reads the active set from kgdb** at startup (or a cached export) and intersects
  it with the loaded matching rules before `match()` — so candidate classes are pre-filtered to
  active leaves. The existing Excel `enabled` flag stays as a second, finer rule-level gate
  (DB activation is coarser, at type granularity); document the precedence (a type inactive in
  the DB wins over an `enabled=TRUE` row).

## Coupling to the persistence catalog seed (P2)

- Extend the catalog seed generator `scripts/gen_kg_catalog_seed.py` (P2) to emit `active`
  per row, with a sensible default and a way to toggle. Recommend `active` defaults so a fresh
  seed reproduces today's behaviour (all currently-`enabled` types active).
- Order the `active` column migration with P2 (same DDL family under
  `media-backend-paid/db/kg_db/`), folded back into `schema.sql` per the schema-first rule.
- Cross-linked from P2 in [`kgdb_event_persistence.md`](kgdb_event_persistence.md).

## Open questions

- **Default activation** for newly seeded types — active or inactive on first seed?
- **Read path** — `Ontology` queries kgdb live at startup vs. consumes a generated active-set
  export refreshed alongside the catalog seed (avoids a DB dependency in the extraction-only
  test harnesses).
- **Does `active` gate downstream too?** Probably transitively yes — only active types are
  extracted, so the linker/persistence never see inactive types — but confirm no path
  (e.g. backfills) bypasses extraction.
- **Excel `enabled` vs DB `active`** — keep both (rule-level vs type-level) or migrate Excel
  `enabled` entirely into the DB once the catalog is authoritative.
