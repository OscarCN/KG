"""Entity linking and deduplication across extracted records."""

from .link import EntityLinker
from .geocode import geocode_location

__all__ = ["EntityLinker", "geocode_location"]
