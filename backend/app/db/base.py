"""SQLAlchemy declarative base for the application schema.

This schema holds *our* concerns: sessions, turns, audit, escalations, refill
requests, and provider usage. Clinical and scheduling data lives in FHIR and is
never copied here (ARCHITECTURE.md section 8). Patients are referenced by FHIR
id only.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

#: Predictable constraint names, so a migration can refer to them later.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
