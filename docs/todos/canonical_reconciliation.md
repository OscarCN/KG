# TODO — Canonical↔canonical reconciliation (consistency pass & multi-match merge)

**Status:** decision pipeline + **merge primitive implemented & validated on dev**; productionization (debt-driven trigger, merge-aware writer, canary) open
**Area:** `scripts/reconcile_dryrun.py` (sweep), `src/entities/linking/merge.py` (primitive), `src/entities/linking/aggregate.py` (shared aggregator), `scripts/reconcile_apply.py` (apply)
**Related:** [`retrieval_linking_per_supertype.md`](retrieval_linking_per_supertype.md), [`location_level_list_extraction.md`](location_level_list_extraction.md), [`../linking.md`](../linking.md)

## Problem

The linker only ever merges an **incoming record into one existing canonical event**. It
never merges two **already-canonical** events together, and `adjudicate()` returns the
**first** matching candidate (the deterministic gate stops at the first shared-leaf+date
hit; the LLM returns a single `match_id`). So once two canonical events for the same
real-world occurrence exist, nothing reconciles them — they are a permanent twin.

**Evidence (geo_qro_paid_mass_event run, 2026-06-16).** "Zona Fest" — one festival at
Estadio Corregidora (~Jun 11–Jul 19) — fragmented into ~18 canonical events. Two of them,
`festival` 586469 (13 sources) and `festival` 445112 (15 sources), share the **identical**
`level_7_id` (`_4842201400010181000020001`) and both start 2026-06-11, yet stayed separate.
They forked because 445112's *seed* source carried a misparsed date (2026-01-06,
`precision_days=42`) whose window didn't overlap 586469 at creation; later 06-11 sources
then merged arbitrarily into one twin or the other (set-iteration order). A coarse/misdated
seed forks a cluster, and nothing ever heals it.

This is **orthogonal to** the supertype-partition change (which addresses fragmentation
*across* `event_type`s — see [`retrieval_linking_per_supertype.md`](retrieval_linking_per_supertype.md)).
Even with a perfect partition, the twin leak remains.

## Two complementary directions to explore

### A. Multi-match merge at link time

When an incoming record matches **more than one** existing canonical event, treat that as
evidence those canonical events are themselves the same, and **merge all of them** (fold the
incoming record + every matched canonical into one survivor).

- Change `adjudicate()` to return the **full set** of matching candidate ids, not just the
  first: the deterministic gate collects *every* candidate sharing a leaf id + date; the LLM
  prompt allows `{"match_ids": [...]}`.
- `link.py` then merges the incoming record into a chosen survivor **and** folds the other
  matched canonicals into it.
- Needs a real **canonical-merge primitive** (`merge_events(into, *others)`): union
  `source_ids` / `_source_windows`, pick the best `date_range`/`location`, and **re-point the
  index** — every key the absorbed events were registered under must now resolve to the
  survivor (or be rewritten). The current `CandidateIndex` is append-only key→ids; absorbing
  an event means either rewriting ids or adding an alias layer (`current_entity_id`-style,
  mirroring `entities_alias` in kgdb).
- Catches twins **the moment** a bridging record arrives — but only if such a record arrives
  and matches both. Doesn't heal twins that no single later record bridges.

### B. Periodic consistency pass (offline reconciliation)

Run a sweep over the current canonical set (every N records, end-of-batch, or scheduled) that
finds canonical events that *should* be one and merges them — independent of any new
incoming record.

- Candidate generation: same partition keys the linker already builds (supertype/event_type,
  geo keys, date windows), but **candidate-vs-candidate** instead of incoming-vs-candidate.
- Decision: reuse the deterministic gate (shared `level_6/7_id` + date overlap) and/or an LLM
  adjudication pass over the canonical pair.
- Same `merge_events` primitive + index re-pointing as (A).
- Heals twins regardless of whether a bridging record ever arrives (would fix the 586469 /
  445112 pair directly). Cost: a periodic O(candidates) sweep; cadence is a tuning knob.

A and B share the hard part — the **canonical-merge primitive + index re-pointing / alias
layer**. Build that once; A calls it inline, B calls it in a sweep.

## Granularity policy (decided 2026-07-12)

Reconciliation obeys a **fine-grained, parent/child-later** event model:

- A specific occurrence (a particular concert, a particular match, an
  **inauguration**) is its **own canonical**; the **umbrella** (the festival, the
  World Cup, a tournament) is **also its own canonical**. The two layers **coexist**
  — never collapse instances into the umbrella nor vice versa. A future parent/child
  relation links them.
- So reconciliation **merges** only records for the *same concrete occurrence*
  (multi-source, name/punctuation/accent variants, descriptor qualifiers like
  "Amistoso México vs Portugal" = "México vs Portugal", date-swap shadows) and
  **splits** umbrella-vs-instance, sibling-instances ("Final M-17" vs "M-20"), and
  inauguration-vs-event.
- Consequence: name **containment** (short ⊂ long) is exactly the umbrella/instance
  boundary → containment-merged groups must route to the LLM, never auto-merge.

## LLM adjudication tier (cost design, from the `scripts/reconcile_dryrun.py` scout)

The deterministic name-led pass (name+geo+date, containment bonus, venue-purity
split) is the **cost filter**: it collapses ~38 K candidate pairs → **67 merge
groups** DB-wide (2 491 events), of which only **~39 are "borderline"** (mixed
names / containment / coarse-bridge) and need the LLM — **100 members total, 80 %
just pairs, 32/39 in `paid_mass_event`** (nameless incident supertypes route ~0).
Design that follows:

1. **Adjudicate groups, never pairs** — the deterministic pass already did the O(pairs) work.
2. **Auto-merge the confident tier for free** — groups whose members are all near-identical
   names (base trigram ≥ `HIGH_TRIGRAM`); ~28 groups, no LLM.
3. **Batch all borderline groups into 1–few LLM calls** — 100 members fit one
   gemini-flash-lite context; one "partition each group into distinct events" prompt →
   whole-DB reconciliation is ~1–3 calls, not 39. The big lever.
4. **Cache per group** (hash of member id-set) so an incremental/streaming sweep only
   re-pays for changed groups — bounded ongoing cost.

The LLM's job is narrow: split sibling-instances / umbrella-vs-instance / inaugurations,
merge descriptor-qualifier and punctuation variants. Two data bugs the scout surfaced feed
false groups: the [day↔month date swap](extraction_date_daymonth_swap.md) (Nov-6 shadows)
and foreign no-geo name collisions ("Los Tigres del Norte en Norfolk" vs "…Greensboro",
neither geocoded → geo gate can't fire; see [`geocode_foreign_out_of_scope.md`](geocode_foreign_out_of_scope.md)).

## Implemented in the dry-run (`scripts/reconcile_dryrun.py`, read-only)

Validated on dev kgdb (2 491 events). Pipeline, in order:

1. **Retrieval** — 4 paths (shared `level_7`/`level_6`, coordinate proximity, coarse→fine
   admin bridge), same supertype.
2. **Components** (recall): `name_score` (trigram **+ containment bonus** with a specificity
   guard) ≥ `NAME_MIN`, hard geo-compatibility, precision-aware date **reject** (only when
   both dates are precise+disjoint). **Venue-purity split** — a component can't span two
   contradicting fine venues (geocoder is authoritative).
3. **Units** (deterministic merge): within a component, near-identical names (base trigram
   ≥ `HIGH_TRIGRAM`, **no** containment) merge — this pre-merges variant-reports so the
   umbrella can't shatter.
4. **LLM** — clusters the (few) multi-unit components' units into events **and tags each
   `umbrella | instance`**. Batched (1 call DB-wide) + cached per component (prompt+model
   keyed). ~38 components need it DB-wide.
5. **Layer-aware, outlier-robust aggregation** — `umbrella` → envelope (min-start..max-end)
   + venue set; `instance` → narrowest window (`_effective_precision_days`) + finest venue;
   both over the **dominant date cluster**, dropping swapped/placeholder-date outliers.

**Result:** Zona Fest's 14 shattered rows → one umbrella with the true 6-week envelope
(`2026-06-01..07-19`, outliers dropped); DB-wide net **−93** canonicals. The two-tier order
(deterministic merge → LLM split/tag) fixed the umbrella-shatter that a record-level "partition
from scratch" LLM caused.

### Residual tuning issues (open)

- **Sibling-instance over-merge — FIXED (deterministic guard).** `Final M-17` / `M-20 varonil`
  / `femenil M-20` folded into one umbrella under *both* `gemini-2.5-flash-lite` and `-flash`
  (model-independent). Fixed by `_are_siblings`: two names are siblings when they share a
  ≥2-token stem but each carries a distinct **number** (M-17 vs M-20, 5a vs 3a) or **gender**
  (varonil vs femenil) discriminator. Applied in **two places** — it vetoes the strict
  unit-merge edge (so siblings never collapse into one deterministic unit) and peels any
  siblings the LLM still merges (post-LLM guarantee). Kept narrow (number/gender only) so
  synonym variation ("Patrimonio Mundial" = "…de la Humanidad") is *not* flagged and still
  merges. Proper-noun-opponent siblings (vs Serbia / vs Sudáfrica) and city siblings (Puebla /
  Querétaro) are left to geo/date + LLM (the latter are already geo-separated). Validated: the
  rugby finals split into 3 instances; Zona Fest umbrella and true merges unaffected.
- **Layer mislabel on single multi-report matches.** A single match reported many times is
  sometimes tagged `umbrella` (e.g. one `México vs Inglaterra`), which over-widens its
  envelope. Tighten the umbrella definition (container-of-distinct-subevents only) or gate the
  umbrella label on ≥2 distinct sub-instances.
- **Same match at two geocoded venues → two events.** Accepted geocoder-trust trade-off
  (venue-purity keeps different `level_7`s apart).

## Merge primitive (direction B execution) — plan locked

Grounded in the kgdb FK topology (`media-backend-paid/db/kg_db/schema.sql`):
`entities_alias.original_entity_id` **and** `current_entity_id` FK → `entities.entity_id`;
`entities_documents`/`entity_types`/`document_extractions` reference
`entities_alias.original_entity_id`; `entity_locations`/`event_properties`/`relations`
reference `entities.entity_id` **directly**.

**Key consequence — tombstone, never delete.** An absorbed `entities` row can't be deleted
(its own alias `original_entity_id` FK points at it). Instead: delete the absorbed's
`event_properties` + `entity_locations` (retrieval JOINs `event_properties`, so the entity
goes **invisible to the linker**), keep the `entities` row, and set
`entities_alias.current_entity_id = survivor` to redirect all references — the mechanism the
alias layer was built for.

**Decisions locked (2026-07-19):**
- **Survivor** = the entity with the most `_sources`; ties → lowest `entity_id`.
- **Children** = **consolidate onto survivor** — rewrite `entities_documents`/`entity_types`/
  `document_extractions` to the survivor's `original_entity_id` (dedupe on conflict; `doc_images`
  NULL-fill), in addition to the mandatory direct-FK moves.

**Build steps:**
0. ✅ **Shared aggregator `linking/aggregate.py`** — `aggregate_date` (layer-aware envelope /
   most-precise over the outlier-robust dominant cluster) + `effective_precision_days`. The dry-run
   and `strategy.py`'s `_effective_precision_days` both delegate to it (single source of truth).
1. ✅ **`CanonicalMerger.merge()` (`linking/merge.py`)** — one transaction: pick survivor (most
   `_sources`, tie→lowest id) → lock `FOR UPDATE` (id-ordered) → union
   `source_ids`/`_source_windows`/`_sources` + layer-aware aggregated `date_range`/`location` +
   `_layer`/`_merged_from` on survivor → survivor `event_properties` aggregated window, delete
   absorbed's → `entity_locations` (umbrella: move distinct venues dedupe geoid; instance: keep
   finest) → **consolidate** alias-routed children (`entities_documents`/`entity_types`/
   `document_extractions`) + `relations`, deduped → `alias.current_entity_id = survivor` →
   tombstone absorbed (`metadata._merged_into`). `dry_run=True` returns the plan without writing.
2. ✅ **Sweep → plan → apply.** `run_pipeline`/`merge_plan` in `reconcile_dryrun.py` expose the
   machine-readable plan; `scripts/reconcile_apply.py` consumes it (**dry-run default /
   `RECON_APPLY=1` execute**, dev via `KGDB_*`), audit-logged to `data/.runlogs/`.
3. ✅ **Validated on dev.** Executed the 3 `protest_event` merges: tombstoned=4, `event_properties`
   2491→2487, absorbed have 0 `event_properties`/`entity_locations` (invisible to retrieval),
   aliases redirect (`2160→2037`), survivor unions source_ids/docs/types + `_layer`/`_merged_from`.
   Sweep now **excludes tombstoned** (`_merged_into IS NULL`) → re-run is a no-op (idempotent).

## Productionization — where wiring B lands us

**Payoff.** The listener is deliberately **single-worker today** ("until reconciliation lands" —
`storage.md`): retrieval → adjudicate → create runs *outside* any DB lock, so two workers can
mint two canonicals for the same event. B is what lifts that constraint →
**N ingestion workers (fast, racy) + self-healing reconciliation**. The permanent-twin leak
closes; dedup becomes eventually-consistent (twins collapse on the next pass).

**Trigger: debt-driven, self-triggered — no cron/scheduler.** Model it like autovacuum/LSM
compaction — the fleet reconciles when there's debt, not on a clock:

- **DB = durable dirty source + audit.** Writer stamps every *created* canonical dirty
  (`entities.reconciled_at = NULL`); a pass clears it for every entity whose neighborhood it
  examined. A `consistency_runs` / merge-log table records each pass + each merge. The whole
  trigger state is SQL-inferable: `count(*) WHERE reconciled_at IS NULL` **is** the debt.
- **Redis = fast trigger cache.** Atomic `INCR recon:debt` per create + a `dirty_partitions`
  set (the created entity's retrieval keys). Cheap per-message; rebuildable from the DB if lost.
- **Opportunistic single-flight.** After a message, a worker checks debt; if `>= N creates`
  (or `M dirty partitions`, or `oldest-dirty age >= T_floor`) it tries a **reconcile lock**
  (Redis `SET NX EX` / PG advisory, TTL'd so a crash self-heals). Lock-holder runs a **bounded
  incremental pass over the dirty neighborhoods**, clears the markers, resets debt, logs a run;
  everyone else keeps ingesting. Single-flight also prevents two sweeps merging one cluster.
  Weight debt higher when a create lands in a partition that already had a recent create
  (likely race-twin). `N` counts *creates* (the real fork risk), not all docs — this is the
  "every N docs" idea, scoped correctly.

**The one hard prerequisite — merge-aware writer.** While a pass tombstones canonical A into
survivor S, a listener worker may be merging a new doc into A (it retrieved A *before* the
merge). Row locks serialize the write, but `upsert_linked` must **resolve the alias**
(`original → current_entity_id` / `_merged_into`) before writing and redirect to S — the same
"resolve alias indirection before insert" `storage.md`'s Direct-FK section already requires.
Without it a concurrent merge can land on a tombstoned row.

**Gap list to production (vs. what's built):**

| Piece | Status |
|---|---|
| Decision quality (units + LLM + sibling guard + layer aggregation) | ✅ dry-run |
| FK topology + tombstone strategy | ✅ settled |
| `CanonicalMerger` primitive + shared `linking/aggregate.py` + `reconcile_apply.py` | ✅ **validated on dev** |
| Merge-aware writer (alias resolution on `upsert_linked`) | ⬜ **concurrency prerequisite** |
| Writer marks creates dirty + `INCR` debt + record dirty partition | ⬜ small add |
| `consistency_runs` / merge-log schema (DDL) | ⬜ |
| Debt check + reconcile lock in the listener loop; incremental pass scoped to dirty set | ⬜ |
| Observability (merge counts, LLM cost, storm/fanout alerts, audit) + canary rollout | ⬜ prod dry-run → review → execute |
| Direction A (link-time multi-match) reusing the primitive | ⬜ later (shrinks the twin window) |

**Residuals (honest).** Eventually-consistent (twins live until the next pass). Reconciliation
is a **safety net, not an extraction fix** — the [date swap](extraction_date_daymonth_swap.md)
and [null `precision_days`](extraction_precision_days_missing.md) bugs keep generating forks
(LLM cost forever); fixing extraction lowers reconciler load. A **confidence gate stays**:
auto-merge the high-confidence tier, log borderline/storm-flagged groups for review rather than
mutating ground truth unattended — especially during canary.

## Open questions

- **Index re-pointing vs. alias indirection.** Rewrite registered ids on merge, or add a
  `current_id` alias map (cheaper, mirrors kgdb `entities_alias.current_entity_id`)? The alias
  route lines up with the eventual kgdb persistence model.
- **Transitivity / merge storms.** Multi-match can chain (A≡B, B≡C ⇒ all one). Bound it; make
  sure one over-eager bridge can't collapse a whole partition (the single-state degeneracy in
  a new guise).
- **Cadence for the consistency pass.** Every N documents, per-batch, or scheduled? Streaming
  (A) + occasional sweep (B) is likely the right combination.
- **Interaction with deterministic-gate weaknesses.** Same-street (level 6) collisions and the
  publication-date leaf rule get *amplified* by canonical-canonical merging — the pass must be
  at least as conservative as the per-record gate (lean on level 7; defer level 6 to the LLM).

## Validation

- Re-run `geo_qro_paid_mass_event`; the 586469 / 445112 festival twins must collapse, and the
  Zona Fest cluster count must drop, **without** over-merging distinct stadium events (e.g. the
  individual World Cup matches, or `detention` 676105 at the same venue).
- Compare canonical count + multi-source-event count before/after; inspect the case log for any
  newly introduced over-merges.
