"""Company and organisation-structure repositories."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import select

from app.core.pagination import PageParams
from app.modules.organization.infrastructure.models import (
    Company,
    Currency,
    Department,
    Designation,
    Holiday,
    Location,
)
from app.shared.database.repository import BaseRepository

#: How deep a department or reporting tree may be walked before we give up.
#: A cycle is already prevented by the service, so hitting this bound means
#: data corrupted by some other route — better to stop than to loop forever.
MAX_HIERARCHY_DEPTH = 64


class CurrencyRepository(BaseRepository[Currency]):
    model = Currency

    async def get_by_code(self, code: str) -> Currency | None:
        stmt = select(Currency).where(Currency.currency_code == code.strip().upper())
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_active(self) -> Sequence[Currency]:
        stmt = select(Currency).where(Currency.is_active.is_(True)).order_by(Currency.currency_code)
        return (await self.session.execute(stmt)).scalars().all()


class CompanyRepository(BaseRepository[Company]):
    model = Company

    async def list_paginated(
        self, params: PageParams, *, is_active: bool | None = None
    ) -> tuple[Sequence[Company], int]:
        stmt = select(Company)
        if is_active is not None:
            stmt = stmt.where(Company.is_active == is_active)
        total = await self.count(stmt)
        stmt = stmt.order_by(Company.name).offset(params.offset).limit(params.limit)
        return (await self.session.execute(stmt)).scalars().all(), total


class DepartmentRepository(BaseRepository[Department]):
    model = Department

    async def get_scoped(self, company_id: UUID, department_id: UUID) -> Department | None:
        """Fetch by id *and* company — the multi-company isolation boundary.

        Every lookup that takes an id from a caller goes through a `_scoped`
        method. Fetching by id alone and checking the company afterwards is
        the mistake that leaks one tenant's data into another's response.
        """
        stmt = select(Department).where(
            Department.id == department_id, Department.company_id == company_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_code(self, company_id: UUID, code: str) -> Department | None:
        stmt = select(Department).where(
            Department.company_id == company_id,
            Department.department_code == code.strip().upper(),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_company(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> tuple[Sequence[Department], int]:
        stmt = select(Department).where(Department.company_id == company_id)
        if is_active is not None:
            stmt = stmt.where(Department.is_active == is_active)
        total = await self.count(stmt)
        stmt = stmt.order_by(Department.department_code).offset(params.offset).limit(params.limit)
        return (await self.session.execute(stmt)).scalars().all(), total

    async def ancestor_ids(self, department_id: UUID) -> list[UUID]:
        """Walk up the parent chain. Feeds the cycle check."""
        ancestors: list[UUID] = []
        current: UUID | None = department_id
        for _ in range(MAX_HIERARCHY_DEPTH):
            if current is None:
                break
            parent = await self.session.scalar(
                select(Department.parent_department_id).where(Department.id == current)
            )
            if parent is None:
                break
            ancestors.append(parent)
            current = parent
        return ancestors


class DesignationRepository(BaseRepository[Designation]):
    model = Designation

    async def get_scoped(self, company_id: UUID, designation_id: UUID) -> Designation | None:
        stmt = select(Designation).where(
            Designation.id == designation_id, Designation.company_id == company_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_code(self, company_id: UUID, code: str) -> Designation | None:
        stmt = select(Designation).where(
            Designation.company_id == company_id,
            Designation.designation_code == code.strip().upper(),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_company(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> tuple[Sequence[Designation], int]:
        stmt = select(Designation).where(Designation.company_id == company_id)
        if is_active is not None:
            stmt = stmt.where(Designation.is_active == is_active)
        total = await self.count(stmt)
        stmt = stmt.order_by(Designation.designation_code).offset(params.offset).limit(params.limit)
        return (await self.session.execute(stmt)).scalars().all(), total


class LocationRepository(BaseRepository[Location]):
    model = Location

    async def get_scoped(self, company_id: UUID, location_id: UUID) -> Location | None:
        stmt = select(Location).where(Location.id == location_id, Location.company_id == company_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_code(self, company_id: UUID, code: str) -> Location | None:
        stmt = select(Location).where(
            Location.company_id == company_id,
            Location.location_code == code.strip().upper(),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_company(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> tuple[Sequence[Location], int]:
        stmt = select(Location).where(Location.company_id == company_id)
        if is_active is not None:
            stmt = stmt.where(Location.is_active == is_active)
        total = await self.count(stmt)
        stmt = stmt.order_by(Location.location_code).offset(params.offset).limit(params.limit)
        return (await self.session.execute(stmt)).scalars().all(), total


class HolidayRepository(BaseRepository[Holiday]):
    model = Holiday

    async def dates_between(
        self,
        company_id: UUID,
        start: date,
        end: date,
        *,
        location_id: UUID | None = None,
    ) -> set[date]:
        """Holiday dates in a range.

        A holiday with no `location_id` applies company-wide; one with a
        location applies only there. Both are returned for an employee at that
        location.
        """
        stmt = select(Holiday.holiday_date).where(
            Holiday.company_id == company_id,
            Holiday.is_active.is_(True),
            Holiday.holiday_date >= start,
            Holiday.holiday_date <= end,
        )
        if location_id is None:
            stmt = stmt.where(Holiday.location_id.is_(None))
        else:
            stmt = stmt.where(
                (Holiday.location_id.is_(None)) | (Holiday.location_id == location_id)
            )
        return set((await self.session.execute(stmt)).scalars().all())

    async def list_for_company(
        self, company_id: UUID, *, year: int | None = None
    ) -> Sequence[Holiday]:
        stmt = select(Holiday).where(Holiday.company_id == company_id)
        if year is not None:
            stmt = stmt.where(
                Holiday.holiday_date >= date(year, 1, 1),
                Holiday.holiday_date <= date(year, 12, 31),
            )
        return (await self.session.execute(stmt.order_by(Holiday.holiday_date))).scalars().all()
