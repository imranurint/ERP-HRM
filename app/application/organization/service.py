"""Organisation-structure services.

Every method takes `company_id` explicitly and every lookup goes through a
`get_scoped` repository method. That is the multi-company isolation boundary:
there is no code path that resolves an id without also asserting the company.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.application.context import ActorContext
from app.application.organization.schemas import (
    DepartmentCreate,
    DepartmentRead,
    DepartmentUpdate,
    DesignationCreate,
    DesignationRead,
    DesignationUpdate,
    HolidayCreate,
    HolidayRead,
    LocationCreate,
    LocationRead,
    LocationUpdate,
)
from app.core.dates import resolve_timezone
from app.core.exceptions import DuplicateResourceError, NotFoundError, ValidationError
from app.core.pagination import Page, PageParams
from app.domain.employees.exceptions import (
    DepartmentNotFoundError,
    DesignationNotFoundError,
    LocationNotFoundError,
)
from app.domain.employees.rules import ensure_no_department_cycle
from app.domain.shared.enums import AuditAction
from app.infrastructure.database.models.organization import (
    Department,
    Designation,
    Holiday,
    Location,
)
from app.infrastructure.database.unit_of_work import UnitOfWork


class OrganizationService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    # ----------------------------------------------------------- departments
    async def create_department(
        self, company_id: UUID, payload: DepartmentCreate, actor: ActorContext
    ) -> DepartmentRead:
        await self._require_company(company_id)
        if await self.uow.departments.get_by_code(company_id, payload.department_code):
            raise DuplicateResourceError(
                "That department code already exists for this company.",
                details={"department_code": payload.department_code},
            )
        if payload.parent_department_id is not None:
            await self._require_department(company_id, payload.parent_department_id)
        if payload.manager_employee_id is not None:
            await self._require_employee(company_id, payload.manager_employee_id)

        department = Department(
            company_id=company_id,
            department_code=payload.department_code,
            name=payload.name,
            description=payload.description,
            parent_department_id=payload.parent_department_id,
            manager_employee_id=payload.manager_employee_id,
            is_active=True,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.departments.add(department)
        await self.uow.flush()

        self._audit(
            AuditAction.DEPARTMENT_CREATED,
            "Department",
            department.id,
            company_id,
            actor,
            new_data={
                "department_code": department.department_code,
                "name": department.name,
                "parent_department_id": _str(department.parent_department_id),
            },
        )
        await self.uow.commit()
        return DepartmentRead.model_validate(department)

    async def update_department(
        self,
        company_id: UUID,
        department_id: UUID,
        payload: DepartmentUpdate,
        actor: ActorContext,
    ) -> DepartmentRead:
        department = await self._require_department(company_id, department_id)
        changes = payload.model_dump(exclude_unset=True)

        if "parent_department_id" in changes and changes["parent_department_id"] is not None:
            parent_id = changes["parent_department_id"]
            await self._require_department(company_id, parent_id)
            # Walk the proposed parent's ancestors: if this department is
            # among them, the assignment would close a loop and every
            # org-chart traversal after it would run forever.
            ancestors = await self.uow.departments.ancestor_ids(parent_id)
            ensure_no_department_cycle(department_id, parent_id, [parent_id, *ancestors])

        if "manager_employee_id" in changes and changes["manager_employee_id"] is not None:
            await self._require_employee(company_id, changes["manager_employee_id"])

        previous = {field: _str(getattr(department, field)) for field in changes}
        for field, value in changes.items():
            setattr(department, field, value)
        department.updated_by = actor.user_id

        self._audit(
            AuditAction.DEPARTMENT_UPDATED,
            "Department",
            department.id,
            company_id,
            actor,
            previous_data=previous,
            new_data={k: _str(v) for k, v in changes.items()},
        )
        await self.uow.commit()
        return DepartmentRead.model_validate(department)

    async def get_department(self, company_id: UUID, department_id: UUID) -> DepartmentRead:
        return DepartmentRead.model_validate(
            await self._require_department(company_id, department_id)
        )

    async def list_departments(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> Page[DepartmentRead]:
        rows, total = await self.uow.departments.list_for_company(
            company_id, params, is_active=is_active
        )
        return Page.build([DepartmentRead.model_validate(r) for r in rows], params, total=total)

    # ---------------------------------------------------------- designations
    async def create_designation(
        self, company_id: UUID, payload: DesignationCreate, actor: ActorContext
    ) -> DesignationRead:
        await self._require_company(company_id)
        if await self.uow.designations.get_by_code(company_id, payload.designation_code):
            raise DuplicateResourceError(
                "That designation code already exists for this company.",
                details={"designation_code": payload.designation_code},
            )

        designation = Designation(
            company_id=company_id,
            designation_code=payload.designation_code,
            name=payload.name,
            description=payload.description,
            grade_level=payload.grade_level,
            is_active=True,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.designations.add(designation)
        await self.uow.flush()

        self._audit(
            AuditAction.DESIGNATION_CREATED,
            "Designation",
            designation.id,
            company_id,
            actor,
            new_data={
                "designation_code": designation.designation_code,
                "name": designation.name,
                "grade_level": designation.grade_level,
            },
        )
        await self.uow.commit()
        return DesignationRead.model_validate(designation)

    async def update_designation(
        self,
        company_id: UUID,
        designation_id: UUID,
        payload: DesignationUpdate,
        actor: ActorContext,
    ) -> DesignationRead:
        designation = await self._require_designation(company_id, designation_id)
        changes = payload.model_dump(exclude_unset=True)
        previous = {field: _str(getattr(designation, field)) for field in changes}
        for field, value in changes.items():
            setattr(designation, field, value)
        designation.updated_by = actor.user_id

        self._audit(
            AuditAction.DESIGNATION_UPDATED,
            "Designation",
            designation.id,
            company_id,
            actor,
            previous_data=previous,
            new_data={k: _str(v) for k, v in changes.items()},
        )
        await self.uow.commit()
        return DesignationRead.model_validate(designation)

    async def list_designations(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> Page[DesignationRead]:
        rows, total = await self.uow.designations.list_for_company(
            company_id, params, is_active=is_active
        )
        return Page.build([DesignationRead.model_validate(r) for r in rows], params, total=total)

    # -------------------------------------------------------------- locations
    async def create_location(
        self, company_id: UUID, payload: LocationCreate, actor: ActorContext
    ) -> LocationRead:
        await self._require_company(company_id)
        if await self.uow.locations.get_by_code(company_id, payload.location_code):
            raise DuplicateResourceError(
                "That location code already exists for this company.",
                details={"location_code": payload.location_code},
            )
        if payload.timezone is not None:
            _validate_timezone(payload.timezone)

        location = Location(
            company_id=company_id,
            location_code=payload.location_code,
            name=payload.name,
            address=payload.address,
            timezone=payload.timezone,
            is_active=True,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.locations.add(location)
        await self.uow.flush()

        self._audit(
            AuditAction.LOCATION_CREATED,
            "Location",
            location.id,
            company_id,
            actor,
            new_data={"location_code": location.location_code, "name": location.name},
        )
        await self.uow.commit()
        return LocationRead.model_validate(location)

    async def update_location(
        self, company_id: UUID, location_id: UUID, payload: LocationUpdate, actor: ActorContext
    ) -> LocationRead:
        location = await self._require_location(company_id, location_id)
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("timezone") is not None:
            _validate_timezone(changes["timezone"])

        previous = {field: _str(getattr(location, field)) for field in changes}
        for field, value in changes.items():
            setattr(location, field, value)
        location.updated_by = actor.user_id

        self._audit(
            AuditAction.LOCATION_UPDATED,
            "Location",
            location.id,
            company_id,
            actor,
            previous_data=previous,
            new_data={k: _str(v) for k, v in changes.items()},
        )
        await self.uow.commit()
        return LocationRead.model_validate(location)

    async def list_locations(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> Page[LocationRead]:
        rows, total = await self.uow.locations.list_for_company(
            company_id, params, is_active=is_active
        )
        return Page.build([LocationRead.model_validate(r) for r in rows], params, total=total)

    # --------------------------------------------------------------- holidays
    async def create_holiday(
        self, company_id: UUID, payload: HolidayCreate, actor: ActorContext
    ) -> HolidayRead:
        await self._require_company(company_id)
        if payload.location_id is not None:
            await self._require_location(company_id, payload.location_id)

        holiday = Holiday(
            company_id=company_id,
            location_id=payload.location_id,
            holiday_date=payload.holiday_date,
            name=payload.name,
            is_active=True,
        )
        self.uow.holidays.add(holiday)
        await self.uow.flush()
        await self.uow.commit()
        return HolidayRead.model_validate(holiday)

    async def list_holidays(
        self, company_id: UUID, *, year: int | None = None
    ) -> list[HolidayRead]:
        rows = await self.uow.holidays.list_for_company(company_id, year=year)
        return [HolidayRead.model_validate(row) for row in rows]

    # ------------------------------------------------------------- internals
    async def _require_company(self, company_id: UUID) -> None:
        if await self.uow.companies.get(company_id) is None:
            raise NotFoundError("Company not found.", details={"company_id": str(company_id)})

    async def _require_department(self, company_id: UUID, department_id: UUID) -> Department:
        row = await self.uow.departments.get_scoped(company_id, department_id)
        if row is None:
            raise DepartmentNotFoundError(details={"department_id": str(department_id)})
        return row

    async def _require_designation(self, company_id: UUID, designation_id: UUID) -> Designation:
        row = await self.uow.designations.get_scoped(company_id, designation_id)
        if row is None:
            raise DesignationNotFoundError(details={"designation_id": str(designation_id)})
        return row

    async def _require_location(self, company_id: UUID, location_id: UUID) -> Location:
        row = await self.uow.locations.get_scoped(company_id, location_id)
        if row is None:
            raise LocationNotFoundError(details={"location_id": str(location_id)})
        return row

    async def _require_employee(self, company_id: UUID, employee_id: UUID) -> None:
        from app.domain.employees.exceptions import EmployeeNotFoundError

        if await self.uow.employees.get_scoped(company_id, employee_id) is None:
            raise EmployeeNotFoundError(details={"employee_id": str(employee_id)})

    def _audit(
        self,
        action: AuditAction,
        entity_type: str,
        entity_id: UUID,
        company_id: UUID,
        actor: ActorContext,
        *,
        previous_data: dict[str, Any] | None = None,
        new_data: dict[str, Any] | None = None,
    ) -> None:
        self.uow.audit.record(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            company_id=company_id,
            user_id=actor.user_id,
            previous_data=previous_data,
            new_data=new_data,
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )


def _validate_timezone(name: str) -> None:
    if str(resolve_timezone(name)) != name and name != "UTC":
        raise ValidationError(
            f"Unknown timezone {name!r}. Use an IANA name such as 'Asia/Manila'.",
            details={"timezone": name},
        )


def _str(value: object) -> object:
    return str(value) if isinstance(value, UUID) else value
