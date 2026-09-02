"""Per-company, per-year document numbering.

Numbers are allocated with a single atomic
`INSERT ... ON CONFLICT DO UPDATE ... RETURNING` inside the surrounding
transaction — never `SELECT MAX()`, never in application memory. See
`app.infrastructure.database.repositories.sequences`.

Gaps are acceptable and expected: a rolled-back transaction never reuses its
number. Reuse would let two different employees or payroll runs share an
identifier over time, which is far worse than a gap.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.shared.enums import DocumentType
from app.infrastructure.database.base import Base, UUIDPrimaryKeyMixin
from app.infrastructure.database.models.mixins import status_enum


class DocumentSequence(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "document_sequences"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    document_type: Mapped[DocumentType] = mapped_column(
        status_enum(DocumentType, "document_type", 32), nullable=False
    )
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    next_number: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "document_type",
            "fiscal_year",
            name="uq_document_sequences_company_id_document_type_fiscal_year",
        ),
        CheckConstraint("next_number >= 1", name="next_number_positive"),
        CheckConstraint("fiscal_year BETWEEN 1900 AND 9999", name="sequence_year_range"),
    )
