# Linking GPT

Generalized in-memory linker for extracted KG records.

## Scope

- **Events** preserve the behavior of `src/entities/linking/`: schema parsing,
  optional geocoding, date-window candidate index, cached LLM disambiguation,
  and merge/reindex behavior.
- **Entities/concepts** are linked with a first-pass strategy: same `entity_type`,
  shared individual name token, then LLM disambiguation using only `name` and
  `description`.
- **Themes** are still skipped.

No Postgres persistence is implemented here.

## Public API

```python
from src.entities.linking_gpt import EntityLinker

linker = EntityLinker(geocode=True)
result = linker.link_one(raw_record)
linked = linker.link_all(records)  # {"events": [...], "entities": [...]}
```

For `tags_gpt`, use the adapter:

```python
from src.entities.linking_gpt import TagsGptLinkingAdapter

adapter = TagsGptLinkingAdapter(event_store=event_store, geocode=True)
tags_result = adapter.link_record(raw_record)
```

The adapter keeps linked entities inside `adapter.linker.entities`, but only
returns linked events to the tags stream for stance/claim tagging.
