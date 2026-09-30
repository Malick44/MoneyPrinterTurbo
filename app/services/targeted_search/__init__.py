"""Caption-first targeted search, with optional acquisition and local models."""

from app.models.search import SearchError
from .repository import Repository
from .service import SearchService
from .settings import Settings

__all__ = ["SearchError", "Repository", "SearchService", "Settings"]
