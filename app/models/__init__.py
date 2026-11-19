"""SQLAlchemy models. Import every model module here so Alembic sees its tables."""

from app.models.base import Base

__all__ = ["Base"]
