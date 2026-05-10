# Prompt Cleanup Handoff

## Context

The current `tags_gpt` prompts have grown from implementation-oriented objects rather than prompt-specific inputs. Several prompts send full customer, event, item, catalog, or assignment records even when the LLM only needs a few fields. This increases token cost, makes cached payloads noisy, and exposes internal IDs/metadata that the model should not need to reason about.

We already applied the cleanup pattern to the two-pass type triage path:

- Customer context is reduced to `{ "name": ..., "description": ... }`.
- Event context is reduced to `{ "description": ... }`.
- Source items are reduced to the smallest shape needed by the prompt:
  `{ "id": <local int>, "kind": ..., "text": ... }` when kind is needed, or numbered text blocks (`[1] text`) when only evidence references are needed.
- Long source IDs are mapped to local integers inside the prompt builder/parser and immediately mapped back after parsing.
- Triage no longer extracts claims and no longer asks for `brief_summary` or `sentiment`.
- Claim extraction is a separate step, limited to articles/posts by default.
- Comment-heavy triage is batched. When comments exist, each batch repeats the article/original post context plus one chunk of comments, so the model can classify comments with the post context present.

The next session should apply the same principle to all remaining prompt builders.

## Core Pattern

For each prompt, define a purpose-specific compact payload instead of reusing broad helper blocks.

1. Send only fields needed for the decision.
2. Replace external IDs with local integer IDs in the prompt.
3. Keep the ID map inside the step that builds and parses the prompt.
4. Map local IDs back to canonical IDs before creating downstream models.
5. Treat unknown local IDs as invalid output and drop them with a counter.
6. Batch large inputs, especially comment-heavy or sample-heavy prompts.
7. Repeat the minimum context needed in each batch, usually article/post context when comments are included.
8. Split unrelated tasks into separate prompts when they need different source scopes or output schemas.

The prompt text should describe the local IDs as opaque references. It should not expose database IDs, full source URLs, full linked event records, full customer records, internal catalog metadata, or unrelated fields unless the task explicitly needs them.

## Prompts To Clean Up

### `stance_tagging_prompt`

Goal: assign typed stance-bearing rows after type triage.

Recommended compact inputs:

- Customer: `name`, `description`.
- Event: `description`.
- Stance type: one explicit type for the call.
- Catalog: only entries for that type, with compact `id`, `label`, `description`, and optional examples.
- Items: local integer `id`, `kind`, `text`.
- Triage hints: local item ID, stance type, importance hint.

Parser requirements:

- Map returned item IDs back to `SourceItem.id`.
- Map returned stance IDs against the type-specific catalog slice.
- Reject assignments whose type does not match the active prompt type.
- Preserve `sentiment` here, because sentiment belongs in stance assignments, not type triage.

Batching:

- Use the same comment batching pattern when comments are included.
- Repeat article/post context with each comment chunk.

### `claim_tagging_prompt`

Goal: extract factual claims from articles/posts, not comments.

Recommended compact inputs:

- Customer: `name`, `description`; include a compact `customer_id` only if the current output schema still requires `affected_entity_ids`.
- Event: `description`.
- Items: local integer `id`, `kind`, `text`, limited to article and post kinds by default.

Parser requirements:

- Map local item IDs back before constructing `RawClaim`.
- Drop unknown local IDs.
- Decide whether the prompt should still ask for `affected_entity_ids`; if so, expose only the minimum customer identifier needed. If not, infer the customer/entity target after parsing.

Batching:

- Usually no comment batching is needed because comments are excluded from claims.
- If article/post text is very long, add text-window batching separately from comment batching.

### `stance_update_prompt`

Goal: adjudicate stance catalog growth and cleanup proposals.

Recommended compact inputs:

- Current catalog entries: compact local or canonical `id`, `label`, `description`, `primary_type`.
- Proposals: local integer proposal IDs, proposed type, label, description, evidence count.
- Optional evidence samples: local sample IDs, item text excerpts, current assignment target.

Parser requirements:

- Map proposal/sample IDs back internally.
- Validate type compatibility before applying updates.
- Keep merge/rename/retire/reroute behavior type-aware.

Batching:

- Batch large proposal lists by type.
- Avoid sending the full assignment history unless the update decision needs it.

### `claim_update_prompt`

Goal: merge or create canonical claim entries from raw claim proposals.

Recommended compact inputs:

- Existing claims: compact `id`, canonical text, importance, small evidence count.
- Incoming raw claims: local integer IDs, verbatim claim text, source kind, source item local ID.
- Event: `description`.

Parser requirements:

- Map local raw claim IDs and source item IDs back before updating the catalog.
- Drop unknown local IDs.

Batching:

- Batch by event and by reasonable raw claim count.
- Send only nearby/possibly related existing claims when possible.

### `consistency_pass_prompt`

Goal: periodic type-aware cleanup of stance assignments and catalog entries.

Recommended compact inputs:

- One stance type per call where possible.
- Catalog slice for that type.
- Assignment samples with local IDs, compact source text, current stance ID/type/sentiment.
- Aggregate counts instead of full histories.

Parser requirements:

- Map sample IDs back internally.
- Validate `add`, `rename`, `merge`, `retire`, and `reroute` actions against the active type.
- Keep tag-only `request` and `noise` separate from stance-bearing catalogs.

Batching:

- Sample and batch by stance type.
- Prefer multiple small consistency calls over one large global prompt.

### bootstrap prompts

Goal: create initial stance catalog entries from a small corpus in two steps.

Recommended compact inputs:

- Customer: `name`, `description`.
- Step 1 type triage: reuse the same batched `TypeTriageStep` used by streaming tagging; no bootstrap-only triage prompt.
- Step 2 per-type catalog bootstrap: only items triaged to the active stance type, rendered as numbered text blocks.
- Active stance type and stance-type guide for the per-type catalog call.

Parser requirements:

- Map local item IDs back only inside the bootstrap step.
- Drop `request` and `noise`; they do not create catalog entries.
- Validate generated entries against the active type.
- Require enough valid local evidence IDs before adding an entry.
- Do not allow catalog bootstrap output to depend on source URLs or source kinds.

Batching:

- Batch corpus items.
- Repeat only the minimal customer/event context in each batch.

## Acceptance Criteria

- Every prompt has its own compact context builder or clearly named compact block.
- Prompt payloads contain local integer IDs for source items, proposals, samples, or raw claims where downstream parsing needs them.
- Parsers map local IDs back before constructing domain models.
- Unknown local IDs are dropped and counted.
- Comment-heavy prompts support a configurable batch size.
- When comments are batched, article/post context is included in each batch if available.
- Claims remain extracted from articles/posts only unless explicitly configured otherwise.
- Prompt smoke tests verify that compact prompts do not include full customer/event/source objects.
- Parser tests cover integer IDs, stringified integer IDs, and unknown IDs.
- Static checks pass:
  - `python -m py_compile src/entities/tags_gpt/*.py src/entities/tags_gpt/prompts/*.py`

## Assumptions

- Default comment batch size remains `12`.
- Local ID mappings are implementation details and should not be stored in catalog state.
- The cleanup should preserve behavior except for prompt shape, token usage, and clearer task separation.
- `request` and `noise` remain tag-only rows with no catalog entry.
- Stance sentiment remains in stance tagging, not type triage.
