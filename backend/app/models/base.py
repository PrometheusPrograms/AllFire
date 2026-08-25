"""Declarative base shared by all SQLAlchemy models.

Alembic's `env.py` imports `Base.metadata` for autogenerate — see
docs/DATA_MODEL.md for the first models to add here.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
