"""Generalized entity linking.

This package keeps the previous event-linking behavior and adds a first
entity/concept linking path. Themes are still skipped.
"""

from src.entities.linking_gpt.link import EntityLinker, LinkResult
from src.entities.linking_gpt.tags_adapter import TagsGptLinkingAdapter

__all__ = [
    "EntityLinker",
    "LinkResult",
    "TagsGptLinkingAdapter",
]
