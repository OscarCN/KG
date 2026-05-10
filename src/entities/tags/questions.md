# Open questions for `tags_design.md` rewrite

Questions to resolve before rewriting `src/entities/tags/tags_design.md`. Answer inline (one line per question) and I'll do a single pass.

---

## A. Streaming unit & event preprocessing

**A1.** Rename `SourceThread` → `ArticleBundle` (matches the existing class name in `tags_gpt`), or keep `SourceThread`? Or a third name?
> Answer: Rename

**A2.** Pre-link step — confirm the design: a separate offline pass walks the extracted_raw fixtures, runs the linker, and writes a sibling fixture file (`data/extracted_with_links/<file>.json` or similar) where each article/post carries its `event_ids: list[str]`. The streaming pipeline reads from THIS pre-linked fixture, never invokes the linker itself. Correct?
> Answer: correct. Our main streaming input item will be articles/posts with their comments. we will run extraction, then linking and then tags on them. the pipeline should add the event_ids to the data item. let's isolate the tags here, assume events are already linked. write a script to do the linking and save the fixtures to be used here.

**A3.** Should the design doc spec the shape of that pre-linked fixture (JSON schema for one article + its embedded comments + its event_ids), or leave that as an implementation detail?
> Answer: only mention that each document (post/article) has fields 'comments', 'event_ids', we have some real raw document data in e.g. data/ayuntamiento_qro/ayuntamiento_qro_20260506_175946.json , obtained by the Poc/get_data.py script

**A4.** An article can have ≥0 linked events. Keep multi-event support, or restrict to ≤1 event per article in v1 to simplify?
> Answer: keep multi event support, and remember that events are never extracted from comments, only articles/posts

---

## B. Claims

**B1.** Strict rule: skip claim extraction entirely for items with zero linked events (no LLM call, no "raw unscoped claims" buffer). Confirm?
> Answer: yes, skip claim extraction when it is not linked

**B2.** Claims only from `article` / `user_post` roots — never `user_comment`. Confirm? (This kills the configurable `include_comments` knob; it goes away.)
> Answer: leave the configurable include_comments, default false

**B3.** If a root has multiple linked events, do we (a) call the claim extractor once per `(root, event)`, (b) call once per root and route claims to events post-hoc, or (c) treat the multi-event case as undefined and pick the first event?
> Answer: call the claim extractor once per (root, event) attach the claim clusters (canonical claim text) of that event for guidance

---

## C. Bootstrap pipeline (the main change you flagged)

**C1.** New bootstrap = three steps:
1. Batched type triage over the customer corpus (same `TypeTriageStep` as streaming).
2. Group all triaged occurrences by `stance_type`.
3. One per-type bootstrap LLM call per stance-bearing type, given that type's full set of occurrences, producing that type's entry-set.

Confirm this is the intended shape?
> Answer: yes

**C2.** The per-type bootstrap call — does it fit in one shot for typical corpora (~hundreds of occurrences per type), or do we batch it too (e.g. iterative cluster-and-merge)?
> Answer: do it one shot

**C3.** Tag-only types (`endorsement`, `noise`) get no bootstrap entries. Confirm?
> Answer: yes

**C4.** `request` — currently §3 says it has a catalog (label shape `"solicitud de X"`). Stays catalog-bearing and gets a bootstrap entry-set, or moves to tag-only like in `tags_legacy/stance_types.md`?
> Answer: it stays catalog-bearing with a bootstrap entry-set

---

## D. Sentiment removal

**D1.** Remove the `sentiment` field from `StanceAssignment`, drop every per-type "Default sentiment" note, and drop sentiment-related rules from the prompts. Confirm?
> Answer: yes, remove sentiment

**D2.** Endorsement currently uses sentiment to carry apoyo-vs-rechazo polarity (it's tag-only with `stance_id = null`, so the only polarity signal IS the sentiment field). Without sentiment, what replaces it?
   - (a) Split into two types: `endorsement_positive` / `endorsement_negative`.
   - (b) Make endorsement catalog-bearing — labels like `"apoyo a X"` vs `"rechazo a X"` carry polarity.
   - (c) Drop endorsement entirely from v1.
   - (d) Keep endorsement tag-only and accept the polarity loss.
> Answer: (b)

---

## E. Stance types & taxonomy

**E1.** Keep all 9 types (entity_stance, complaint, gratefulness, suggestion, request, denuncia, question, endorsement, noise) — modulo the endorsement decision in D2?
> Answer: yes

**E2.** Streaming-time growth (Phase 2 can propose `add`) — keep restricted to entity_stance only, or extend to other types?
> Answer: extend to all types, all types can `add` and `update` at streaming-time

**E3.** The §3 per-type tables today have 4 examples each. Keep at this length, expand toward the richness of `tags_legacy/stance_types.md` (5-6 examples + cross-type contrast notes), or trim further?
> Answer: 3-4 examples should suffice, depending on the complexity, (endorsement shouldn't need many examples, whereas e.g. denuncias, complaints, etc can be implicit in the text)

**E4.** Multi-stance per item (one source item → multiple typed assignments) — keep as a v1 feature, or defer?
> Answer: keep as v1

---

## F. Consistency pass / consolidation

**F1.** Is the consistency pass in scope for v1, or deferred to v2? (Your "lean" framing suggests deferring; let me know.)
> Answer: it is in scope

**F2.** If deferred: drop `consistency_relevance`, `consistency_used`, `last_consistency_pass_*`, and the worthiness flag from the design entirely (delete §5.7, §7-style fields, and the related Customer state)?
> Answer:

**F3.** If kept: same shape as currently described, or simpler?
> Answer: simplify if possible 

---

## G. Doc cleanup & structure

**G1.** Replace every `tags_gpt` module reference in the doc with `tags` (since the new implementation lives under `src/entities/tags/`). Confirm the rename target is just `tags`, not e.g. `tags_v2`?
> Answer: yes, leave the legacy code out of documentation and elsewhere

**G2.** §6 "Prompting Rules" — keep but trim to essentials (compact context, local-id mapping, batching, prompt separation, validation), or drop entirely and let the prompt files speak for themselves?
> Answer: keep

**G3.** §7 "Data Model Summary" — keep the dataclass shapes (lean), or move them to a sibling file?
> Answer: move to a sibling file

**G4.** §9 "Implementation Direction" — keep as a 7-class service split, or trim/remove?
> Answer: keep

**G5.** Add a new "Test fixtures & local step-by-step run" section showing how to (a) build the pre-linked fixture, (b) run bootstrap, (c) run streaming, (d) inspect snapshots? This is what makes "test everything step by step" possible.
> Answer: yes

**G6.** Add a brief "Stage 2 — database coupling" section that maps each in-memory class to a postgres table and points at `media-backend-paid/docs/DATABASE_POSTGRES.md`?
> Answer: yes

**G7.** Drop the "Pointers" section (currently points to `tags_overview.md` / `tags_impl_plan.md` which don't exist in the new tags/ dir)?
> Answer: yes, drop

---

## H. Prompts (separate from this doc, but I want confirmation on scope)

**H1.** The prompts in `tags_gpt/prompts/text/` we just rewrote are now stale relative to this new design (sentiment removed, two-pass bootstrap, claims-require-event). Plan: keep this round focused on `tags_design.md` only; prompts get rewritten in a follow-up under `tags/prompts/` once the design lands. Confirm?
> Answer: yes, prompts get rewritten in a follow-up under `tags/prompts/` once the design lands

**H2.** When we do rewrite them, target fewer/simpler prompts with less context per call — drop the rich rubric/examples blocks where they aren't load-bearing, and rely on per-type label-shape exemplars only. Direction sound?
> Answer: yes, let's also just do them leaner and more to the point, e.g. in the type_triage prompt '''Rol
Eres un clasificador ligero (NO etiquetador de catálogo).
Tu salida es un manifiesto que enruta el siguiente paso typed: solo los items con un stance_type que carga catálogo ...''' wtf is that? we can just say, classify each item into zero to many of the next catalogue, .... and add descriptions and examples and output format
