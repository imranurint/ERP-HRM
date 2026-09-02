"""Atomic document number allocation.

Employee numbers, contract numbers and payroll run numbers must be unique per
company and year, allocated without a `SELECT MAX()` and without any
application-side counter. A single statement does it:

    INSERT INTO document_sequences (..., next_number) VALUES (..., 2)
    ON CONFLICT (company_id, document_type, fiscal_year)
    DO UPDATE SET next_number = document_sequences.next_number + 1
    RETURNING next_number - 1

On first use the row is created with `next_number = 2` and 1 is returned. On
every later call PostgreSQL takes a row lock for the UPDATE, increments, and
returns the previous value. Concurrent callers serialise on that row only, and
no two transactions can ever be handed the same number.

Numbering policy: gaps are possible and acceptable. If the surrounding
transaction rolls back, the allocated number is simply never used — the
alternative (reusing numbers) would let two different employees share a number
over time, which is far worse for an HR record.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.ids import uuid7
from app.domain.shared.enums import DocumentType

_ALLOCATE_SQL = text(
    """
    INSERT INTO document_sequences
        (id, company_id, document_type, fiscal_year, next_number, updated_at)
    VALUES
        (:id, :company_id, :document_type, :fiscal_year, 2, now())
    ON CONFLICT (company_id, document_type, fiscal_year)
    DO UPDATE SET
        next_number = document_sequences.next_number + 1,
        updated_at  = now()
    RETURNING document_sequences.next_number - 1 AS allocated
    """
)


def _prefix(document_type: DocumentType) -> str:
    """Read prefixes lazily so a settings override in tests is respected."""
    match document_type:
        case DocumentType.EMPLOYEE:
            return settings.employee_number_prefix
        case DocumentType.CONTRACT:
            return settings.contract_number_prefix
        case DocumentType.PAYROLL_RUN:
            return settings.payroll_run_number_prefix


class DocumentSequenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def allocate(
        self, company_id: UUID, fiscal_year: int, document_type: DocumentType
    ) -> int:
        """Reserve and return the next sequence number. Caller must be in a txn."""
        result = await self.session.execute(
            _ALLOCATE_SQL,
            {
                "id": uuid7(),
                "company_id": company_id,
                "document_type": document_type.value,
                "fiscal_year": fiscal_year,
            },
        )
        return int(result.scalar_one())

    async def allocate_number(
        self, company_id: UUID, fiscal_year: int, document_type: DocumentType
    ) -> str:
        """Return a formatted document number, e.g. `EMP-2026-000001`."""
        number = await self.allocate(company_id, fiscal_year, document_type)
        return format_number(document_type, fiscal_year, number)

    async def allocate_employee_number(self, company_id: UUID, fiscal_year: int) -> str:
        return await self.allocate_number(company_id, fiscal_year, DocumentType.EMPLOYEE)

    async def allocate_contract_number(self, company_id: UUID, fiscal_year: int) -> str:
        return await self.allocate_number(company_id, fiscal_year, DocumentType.CONTRACT)

    async def allocate_payroll_run_number(self, company_id: UUID, fiscal_year: int) -> str:
        return await self.allocate_number(company_id, fiscal_year, DocumentType.PAYROLL_RUN)

    async def peek(self, company_id: UUID, fiscal_year: int, document_type: DocumentType) -> int:
        """The number the next allocation would return. Diagnostics only."""
        stmt = text(
            """
            SELECT next_number FROM document_sequences
            WHERE company_id = :company_id
              AND document_type = :document_type
              AND fiscal_year = :fiscal_year
            """
        )
        value = await self.session.scalar(
            stmt,
            {
                "company_id": company_id,
                "document_type": document_type.value,
                "fiscal_year": fiscal_year,
            },
        )
        return int(value) if value is not None else 1


def format_number(document_type: DocumentType, fiscal_year: int, number: int) -> str:
    return f"{_prefix(document_type)}-{fiscal_year}-{number:06d}"
