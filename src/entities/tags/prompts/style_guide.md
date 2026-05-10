# Tags prompts — style guide

Patterns extracted from the `triage.txt` rewrite. Apply to every prompt
under this directory when rewriting them. Each pattern has a one-line
rule + the anti-pattern it kills.

---

## 1. Don't leak pipeline architecture into the prompt

**Rule.** The model only sees this prompt; it has no other tools and no
context about the rest of the system. Don't tell it what other steps
exist, what this step is *not* doing, or what phase it belongs to.

**Anti-pattern.**
```
Esta fase NO elige entradas de catálogo y NO extrae claims — sólo …
```
**Why it's wrong.** The model wasn't going to do those things; it has no
tools for them. Pure filler tokens.

**Fix.** Just describe the task. If a temptation is real (e.g. "don't
include a `sentiment` field" — a field the model knows from training),
keep the negative. Otherwise drop it.

---

## 2. Don't leak pipeline names

**Rule.** Internal class / step / phase names are code-side vocabulary.
The prompt should use **domain terms**, not architecture terms.

**Examples.**
- ❌ `"triage": [...]` (wrapper key) → ✅ `"rows": [...]` (what each entry IS)
- ❌ "the triage step classifies …" → ✅ "Clasifica cada item …"
- ❌ "tag-only types" → ✅ "items que se omiten"

---

## 3. Anchor every block with its role

**Rule.** Every block injection (`{customer}`, `{event}`, `{items}`, etc.)
needs a one-line label that tells the model **how to use** the block, not
just what it contains.

**Anti-pattern.**
```
CLIENTE: {customer}
EVENTO: {event}
```
**Fix.**
```
CLIENTE (entidad principal — sólo cuentan las posturas que le aplican directa o indirectamente):
{customer}

EVENTO (contexto opcional):
{event}
```

---

## 4. Make the customer-relevance gate explicit

**Rule.** Whenever a prompt accepts a customer block, add a rule that
says "if the input doesn't relate to this customer, drop it / omit it /
return empty". Otherwise the model classifies everything because
classification is what it was asked for.

**Concrete rule for triage / claim extract / stance tagging.**
> Si un item no afecta la percepción del cliente, omítelo (no emitas
> filas para él).

---

## 5. Audit every output field for downstream consumers

**Rule.** Before adding a field to the JSON schema, grep the codebase
for actual readers. If nothing consumes the value, drop the field.

**Process.**
```bash
grep -rnE "<field_name>" src/entities/tags/ scripts/
```

If the only references are: the dataclass declaration, the prompt
itself, the parser that writes the dataclass, and the design doc — the
field has **no consumer** and should be dropped.

If a real consumer exists (`streaming.py:206`, `bootstrap.py:127`, etc.),
keep the field and document the consumer in the prompt's field
description so future editors can see why it's there.

**Mistake to avoid.** Don't drop a field based on "I don't see it being
used" — actually grep. We dropped `brief_summary` once thinking it had
no consumer; it had two.

---

## 6. Absorption categories should OMIT, not EMIT

**Rule.** If a category exists only to swallow inputs the rest of the
pipeline ignores (e.g. `noise`, `off_topic`, `unrelated`), don't ask
the model to emit a row for those inputs — ask it to **omit** them.

**Why.**
- Saves output tokens (~25–40 per omitted row).
- Saves persistence rows (no `noise` assignments stored).
- Coverage is still recoverable: `items_seen − items_with_rows = absorbed_count`.

**When to keep an absorption row.** Only when something downstream needs
the row's metadata (a per-row reason, a counter you can't derive
otherwise, an audit trail that has to be persisted).

---

## 7. Short JSON keys at the LLM boundary

**Rule.** Use short keys in the prompt's JSON schema. Keep dataclass
field names long for clarity in code. Translate at the parser.

**Mapping convention.**
| Long (code) | Short (prompt) |
|---|---|
| `source_item_id` | `id` |
| `stance_type` | `type` |
| `brief_summary` | `summary` |
| `importance_hint` | `importance` |
| `stance_id` | `stance_id` (already short) |
| `cluster_id` | `cluster_id` (already short) |
| `claim_index` | `idx` (when room) |

**Parser pattern (backward-compat with cached responses).**
```python
local_id = raw.get("id", raw.get("source_item_id"))
stype    = raw.get("type", raw.get("stance_type"))
```

The fallback to the long key keeps any cached LLM response from
poisoning a re-run after a key rename.

---

## 8. Wrapper key names what each entry IS, not what step produced it

**Rule.** Top-level JSON wrapper key should be a noun describing the
content, not the step.

**Examples.**
- ❌ `"triage": [...]` → ✅ `"rows": [...]` (each entry = one classification row)
- ❌ `"adjudication": [...]` → ✅ `"decisions": [...]`
- ❌ `"clustering": [...]` → ✅ `"decisions": [...]` + `"mutations": [...]`

---

## 9. Pick one domain term and stick to it

**Rule.** Don't switch between near-synonyms in prose ("idea" vs
"postura", "claim" vs "afirmación", "entry" vs "etiqueta").

**Mistake to avoid.** Opening line says "posturas que expresa" → rules
section says "Una fila por idea distinta". The model parses two terms
as potentially different things. Use one.

---

## 10. Per-field semantics inline next to the schema

**Rule.** Don't enumerate semantics in a separate "Campos de salida"
block above the schema. Put them inline as a one-liner per field, next
to the schema example.

**Anti-pattern.**
```
REGLAS
- ...
- ...

Campos de salida:
- source_item_id: …
- stance_type: …
- summary: …
- importance: …

Responde con JSON:
{...}
```
**Fix.** Keep the rules as constraints on behavior. Define field
semantics in one tight block right before the schema:
```
summary: frase corta (≤ 10 palabras, español de México) …
importance: high = …; medium = …; low = …

Responde EXCLUSIVAMENTE con JSON:
{...}
```

---

## 11. Show the edge case in the example

**Rule.** The output example should demonstrate the non-trivial behaviors
the rules describe — multi-row per item, omission, empty wrapper.

**Concrete.**
```
Ejemplo (item 1 lleva dos posturas; item 2 es saludo y por eso se omite):
{
  "rows": [
    {"id": 1, "type": "gratefulness", ...},
    {"id": 1, "type": "suggestion",   ...}
  ]
}

Si nada aplica, devuelve {"rows": []}.
```

The annotation in parentheses shows *why* the example looks the way it
does, not just *what* the JSON looks like.

---

## 12. Don't repeat constraints

**Rule.** Each constraint stated once. Catalogue rules, field semantics,
output examples each cover their own scope without duplicating.

**Anti-pattern.** "Tope suave: 4 filas por item" appearing in both the
rules block and the field semantics block.

---

## 13. Type-guide injection has to track type-emit changes

**Rule.** When you drop a type from the emissible set (e.g. `noise` is
omitted, not emitted), the per-type guide file must reflect that — not
just the main prompt.

**Concrete.** When `noise` stopped being an output type, the prompt
dropped it from the tie-break list, but the type guide (`types/noise.txt`)
also had to be updated to say "this type is OMITTED, not emitted",
otherwise the injected guide still teaches the model how to write a
noise row.

---

## 14. Backward-compat at the parser, not the prompt

**Rule.** When renaming JSON fields, the **parser** keeps fallbacks for
the old names; the **prompt** doesn't mention them. Keeps the prompt
clean and avoids contradicting yourself.

**Pattern.**
```python
# parser
local_id = raw.get("id", raw.get("source_item_id"))

# prompt
{"id": 1, "type": "complaint", ...}    # only the new name
```

---

## Application checklist for each remaining prompt

When rewriting `bootstrap_per_type.txt`, `tag_per_type.txt`,
`claim_extract.txt`, `claim_group.txt`, `consistency_per_type.txt`:

- [ ] All injected blocks have a role-label, not a bare noun.
- [ ] Customer-relevance gate present (where applicable).
- [ ] No "this phase doesn't X" lines.
- [ ] No internal class / step / phase names in prose or wrapper keys.
- [ ] Every output field has at least one downstream consumer (verified
      by grep).
- [ ] Absorption categories use OMIT, not EMIT.
- [ ] JSON keys shortened at the LLM boundary; parser keeps fallbacks.
- [ ] Wrapper key is content-named, not step-named.
- [ ] Domain vocabulary consistent across the prompt.
- [ ] Field semantics inline near the schema, not in a separate block.
- [ ] Example output demonstrates at least one edge case.
- [ ] No constraint repeated in two places.
- [ ] Type-guide files (`types/<type>.txt`) reflect any type-emit changes.
