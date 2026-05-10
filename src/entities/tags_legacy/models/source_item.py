"""SourceItem — a uniform representation of articles, posts, and comments.

The same dataclass holds all three kinds; downstream code disambiguates
via `kind` and (for comments) `parent_source_id`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


SourceKind = Literal["article", "user_post", "user_comment"]


@dataclass
class SourceItem:
    id: str
    kind: SourceKind
    text: str
    author: Optional[str] = None
    created_at: Optional[str] = None
    parent_source_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "author": self.author,
            "created_at": self.created_at,
            "parent_source_id": self.parent_source_id,
            "metadata": self.metadata,
        }
