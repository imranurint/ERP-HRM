"""Employee lifecycle service.

Every lifecycle transition follows the same shape, and it is deliberate:

    lock the row → validate the transition → capture before/after →
    mutate → write a lifecycle event → write an audit row → commit once

The row lock is what makes it safe: termination and status changes are
read-then-write, so two concurrent terminations without a lock would both pass
the transition check and produce two lifecycle events for one departure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from app.core.dates import business_today
from app.core.exceptions import ConcurrencyError, NotFoundError, ValidationError
from app.core.pagination import Page, PageParams
from app.modules.employees.application.schemas import (
    ActivateRequest,
    EmployeeCreate,
    EmployeeRead,
    EmployeeUpdate,
    LifecycleEventRead,
    PromoteRequest,
    SuspendRequest,
    TerminateRequest,
    TransferRequest,
)
from app.modules.employees.domain.enums import EmploymentStatus, Gender, LifecycleEventType
from app.modules.employees.domain.exceptions import (
    DuplicateEmployeeNumberError,
    DuplicateWorkEmailError,
    EmployeeNotFoundError,
    InvalidManagerError,
)
from app.modules.employees.domain.rules import (
    ensure_date_of_birth,
    ensure_employed,
    ensure_hire_date,
    ensure_no_manager_cycle,
    ensure_status_transition,
    ensure_termination_date,
)
from app.modules.employees.infrastructure.models import Employee, EmployeeLifecycleEvent
from app.shared.context import ActorContext
from app.shared.enums import AuditAction
from app.shared.events.domain_events import (
    EmployeeHired,
    EmployeeStatusChanged,
    EmployeeTransferred,
)
from app.shared.observability import metrics
from app.shared.unit_of_work.unit_of_work import UnitOfWork

#: Status → the lifecycle event and audit action it produces.
_STATUS_EVENTS: dict[EmploymentStatus, tuple[LifecycleEventType, AuditAction]] = {
    EmploymentStatus.ACTIVE: (LifecycleEventType.REINSTATED, AuditAction.EMPLOYEE_ACTIVATED),
    EmploymentStatus.SUSPENDED: (LifecycleEventType.SUSPENDED, AuditAction.EMPLOYEE_SUSPENDED),
    EmploymentStatus.ON_LEAVE: (LifecycleEventType.ON_LEAVE, AuditAction.EMPLOYEE_UPDATED),
    EmploymentStatus.TERMINATED: (LifecycleEventType.TERMINATED, AuditAction.EMPLOYEE_TERMINATED),
    EmploymentStatus.RESIGNED: (LifecycleEventType.RESIGNED, AuditAction.EMPLOYEE_RESIGNED),
    EmploymentStatus.INACTIVE: (LifecycleEventType.TERMINATED, AuditAction.EMPLOYEE_UPDATED),
}


@dataclass(frozen=True, slots=True)
class EmployeeFilters:
    department_id: UUID | None = None
    designation_id: UUID | None = None
    location_id: UUID | None = None
    manager_id: UUID | None = None
    employment_status: EmploymentStatus | None = None
    is_active: bool | None = None
    search: str | None = None


class EmployeeService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    # ---------------------------------------------------------------- create
    async def create(
        self, company_id: UUID, payload: EmployeeCreate, actor: ActorContext
    ) -> EmployeeRead:
        company = await self.uow.companies.get(company_id)
        if company is None:
            raise NotFoundError("Company not found.", details={"company_id": str(company_id)})

        today = business_today(company.timezone)
        ensure_hire_date(payload.hire_date, today=today)
        if payload.date_of_birth is not None:
            ensure_date_of_birth(payload.date_of_birth, payload.hire_date)

        await self._validate_org_references(
            company_id,
            department_id=payload.department_id,
            designation_id=payload.designation_id,
            location_id=payload.location_id,
        )
        if payload.manager_id is not None:
            await self._require_employee(company_id, payload.manager_id)

        employee_number = payload.employee_number
        if employee_number:
            employee_number = employee_number.strip().upper()
            if await self.uow.employees.get_by_number(company_id, employee_number):
                raise DuplicateEmployeeNumberError(details={"employee_number": employee_number})
        else:
            # Atomic INSERT ... ON CONFLICT DO UPDATE ... RETURNING — never
            # SELECT MAX(). Two simultaneous hires cannot collide.
            employee_number = await self.uow.sequences.allocate_employee_number(
                company_id, payload.hire_date.year
            )

        if await self.uow.employees.get_by_work_email(company_id, str(payload.work_email)):
            raise DuplicateWorkEmailError(details={"work_email": str(payload.work_email)})

        status = payload.employment_status
        employee = Employee(
            company_id=company_id,
            employee_number=employee_number,
            first_name=payload.first_name,
            last_name=payload.last_name,
            work_email=str(payload.work_email).lower(),
            personal_email=str(payload.personal_email).lower() if payload.personal_email else None,
            work_phone=payload.work_phone,
            personal_phone=payload.personal_phone,
            date_of_birth=payload.date_of_birth,
            gender=payload.gender or Gender.UNDISCLOSED,
            nationality=payload.nationality,
            hire_date=payload.hire_date,
            employment_status=status,
            department_id=payload.department_id,
            designation_id=payload.designation_id,
            manager_id=payload.manager_id,
            location_id=payload.location_id,
            is_active=_active_flag(status),
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.employees.add(employee)
        await self.uow.flush()

        self._lifecycle(
            employee,
            LifecycleEventType.HIRED,
            payload.hire_date,
            actor,
            new_data=self._snapshot(employee),
        )
        self._audit(
            AuditAction.EMPLOYEE_CREATED,
            employee,
            actor,
            new_data=self._snapshot(employee),
        )
        self.uow.outbox.enqueue(
            EmployeeHired(
                aggregate_id=employee.id,
                company_id=company_id,
                employee_number=employee.employee_number,
                full_name=employee.full_name,
                hire_date=employee.hire_date,
                department_id=employee.department_id,
                designation_id=employee.designation_id,
                created_by=actor.user_id,
            )
        )
        await self.uow.commit()
        await self.uow.refresh(employee)

        metrics.employee_lifecycle_events_total.labels(
            str(company_id), str(LifecycleEventType.HIRED)
        ).inc()
        return EmployeeRead.model_validate(employee)

    # ------------------------------------------------------------------ read
    async def get(self, company_id: UUID, employee_id: UUID) -> EmployeeRead:
        return EmployeeRead.model_validate(await self._require_employee(company_id, employee_id))

    async def list_page(
        self, company_id: UUID, params: PageParams, filters: EmployeeFilters
    ) -> Page[EmployeeRead]:
        rows, total = await self.uow.employees.list_paginated(
            company_id,
            params,
            department_id=filters.department_id,
            designation_id=filters.designation_id,
            location_id=filters.location_id,
            manager_id=filters.manager_id,
            employment_status=filters.employment_status,
            is_active=filters.is_active,
            search=filters.search,
        )
        return Page.build([EmployeeRead.model_validate(r) for r in rows], params, total=total)

    async def lifecycle_history(
        self, company_id: UUID, employee_id: UUID, params: PageParams
    ) -> Page[LifecycleEventRead]:
        await self._require_employee(company_id, employee_id)
        rows, total = await self.uow.lifecycle.list_for_employee(employee_id, params)
        return Page.build([LifecycleEventRead.model_validate(r) for r in rows], params, total=total)

    # ---------------------------------------------------------------- update
    async def update(
        self, company_id: UUID, employee_id: UUID, payload: EmployeeUpdate, actor: ActorContext
    ) -> EmployeeRead:
        employee = await self._require_employee_locked(company_id, employee_id)
        changes = payload.model_dump(exclude_unset=True)
        expected_version = changes.pop("version", None)
        if expected_version is not None and expected_version != employee.version:
            raise ConcurrencyError(
                "This employee was modified by another request.",
                details={"expected_version": expected_version, "actual_version": employee.version},
            )

        if changes.get("location_id") is not None:
            await self._validate_org_references(company_id, location_id=changes["location_id"])
        if "work_email" in changes and changes["work_email"] is not None:
            changes["work_email"] = str(changes["work_email"]).lower()
            existing = await self.uow.employees.get_by_work_email(company_id, changes["work_email"])
            if existing is not None and existing.id != employee.id:
                raise DuplicateWorkEmailError(details={"work_email": changes["work_email"]})
        if "personal_email" in changes and changes["personal_email"] is not None:
            changes["personal_email"] = str(changes["personal_email"]).lower()

        birth = changes.get("date_of_birth")
        if birth is not None:
            ensure_date_of_birth(birth, employee.hire_date)

        previous = {field: _jsonable(getattr(employee, field)) for field in changes}
        for field, value in changes.items():
            setattr(employee, field, value)
        employee.updated_by = actor.user_id
        employee.version += 1

        self._audit(
            AuditAction.EMPLOYEE_UPDATED,
            employee,
            actor,
            previous_data=previous,
            new_data={k: _jsonable(v) for k, v in changes.items()},
        )
        await self.uow.commit()
        return EmployeeRead.model_validate(employee)

    # ------------------------------------------------------------- lifecycle
    async def activate(
        self, company_id: UUID, employee_id: UUID, payload: ActivateRequest, actor: ActorContext
    ) -> EmployeeRead:
        """Confirm an employee into ACTIVE — from probation, leave or suspension."""
        return await self._transition(
            company_id,
            employee_id,
            EmploymentStatus.ACTIVE,
            actor,
            effective_date=payload.effective_date,
            reason=payload.reason,
        )

    async def suspend(
        self, company_id: UUID, employee_id: UUID, payload: SuspendRequest, actor: ActorContext
    ) -> EmployeeRead:
        return await self._transition(
            company_id,
            employee_id,
            EmploymentStatus.SUSPENDED,
            actor,
            effective_date=payload.effective_date,
            reason=payload.reason,
        )

    async def terminate(
        self, company_id: UUID, employee_id: UUID, payload: TerminateRequest, actor: ActorContext
    ) -> EmployeeRead:
        """End employment.

        The employee row is never deleted: attendance, leave and payslip
        history must stay joinable, and in most jurisdictions retained.
        """
        target = EmploymentStatus.RESIGNED if payload.resigned else EmploymentStatus.TERMINATED
        return await self._transition(
            company_id,
            employee_id,
            target,
            actor,
            effective_date=payload.termination_date,
            reason=payload.reason,
            termination_date=payload.termination_date,
        )

    async def transfer(
        self, company_id: UUID, employee_id: UUID, payload: TransferRequest, actor: ActorContext
    ) -> EmployeeRead:
        employee = await self._require_employee_locked(company_id, employee_id)
        ensure_employed(employee.employment_status, action="transfer this employee")

        if (
            payload.department_id is None
            and payload.location_id is None
            and (payload.manager_id is None)
        ):
            raise ValidationError(
                "A transfer must change at least one of department, location or manager."
            )

        await self._validate_org_references(
            company_id,
            department_id=payload.department_id,
            location_id=payload.location_id,
        )
        if payload.manager_id is not None:
            await self._validate_manager(company_id, employee.id, payload.manager_id)

        effective = payload.effective_date or await self._today(company_id)
        previous = {
            "department_id": _jsonable(employee.department_id),
            "location_id": _jsonable(employee.location_id),
            "manager_id": _jsonable(employee.manager_id),
        }
        previous_department = employee.department_id
        previous_location = employee.location_id

        if payload.department_id is not None:
            employee.department_id = payload.department_id
        if payload.location_id is not None:
            employee.location_id = payload.location_id
        if payload.manager_id is not None:
            employee.manager_id = payload.manager_id
        employee.updated_by = actor.user_id
        employee.version += 1

        new_data = {
            "department_id": _jsonable(employee.department_id),
            "location_id": _jsonable(employee.location_id),
            "manager_id": _jsonable(employee.manager_id),
        }
        self._lifecycle(
            employee,
            LifecycleEventType.TRANSFERRED,
            effective,
            actor,
            previous_data=previous,
            new_data=new_data,
            reason=payload.reason,
        )
        self._audit(
            AuditAction.EMPLOYEE_TRANSFERRED,
            employee,
            actor,
            previous_data=previous,
            new_data=new_data,
        )
        self.uow.outbox.enqueue(
            EmployeeTransferred(
                aggregate_id=employee.id,
                company_id=company_id,
                employee_number=employee.employee_number,
                previous_department_id=previous_department,
                new_department_id=employee.department_id,
                previous_location_id=previous_location,
                new_location_id=employee.location_id,
                effective_date=effective,
                changed_by=actor.user_id,
            )
        )
        await self.uow.commit()

        metrics.employee_lifecycle_events_total.labels(
            str(company_id), str(LifecycleEventType.TRANSFERRED)
        ).inc()
        return EmployeeRead.model_validate(employee)

    async def promote(
        self, company_id: UUID, employee_id: UUID, payload: PromoteRequest, actor: ActorContext
    ) -> EmployeeRead:
        employee = await self._require_employee_locked(company_id, employee_id)
        ensure_employed(employee.employment_status, action="promote this employee")

        designation = await self.uow.designations.get_scoped(company_id, payload.designation_id)
        if designation is None:
            from app.modules.employees.domain.exceptions import DesignationNotFoundError

            raise DesignationNotFoundError(details={"designation_id": str(payload.designation_id)})
        if payload.manager_id is not None:
            await self._validate_manager(company_id, employee.id, payload.manager_id)

        # A "promotion" to the same designation is a no-op that would pollute
        # the lifecycle history with a meaningless event.
        if employee.designation_id == payload.designation_id and payload.manager_id is None:
            raise ValidationError(
                "The employee already holds that designation.",
                details={"designation_id": str(payload.designation_id)},
            )

        effective = payload.effective_date or await self._today(company_id)
        previous = {
            "designation_id": _jsonable(employee.designation_id),
            "manager_id": _jsonable(employee.manager_id),
        }
        employee.designation_id = payload.designation_id
        if payload.manager_id is not None:
            employee.manager_id = payload.manager_id
        employee.updated_by = actor.user_id
        employee.version += 1

        new_data = {
            "designation_id": _jsonable(employee.designation_id),
            "designation_code": designation.designation_code,
            "manager_id": _jsonable(employee.manager_id),
        }
        self._lifecycle(
            employee,
            LifecycleEventType.PROMOTED,
            effective,
            actor,
            previous_data=previous,
            new_data=new_data,
            reason=payload.reason,
        )
        self._audit(
            AuditAction.EMPLOYEE_PROMOTED,
            employee,
            actor,
            previous_data=previous,
            new_data=new_data,
        )
        await self.uow.commit()

        metrics.employee_lifecycle_events_total.labels(
            str(company_id), str(LifecycleEventType.PROMOTED)
        ).inc()
        return EmployeeRead.model_validate(employee)

    # ------------------------------------------------------------- internals
    async def _transition(
        self,
        company_id: UUID,
        employee_id: UUID,
        target: EmploymentStatus,
        actor: ActorContext,
        *,
        effective_date: date | None,
        reason: str | None,
        termination_date: date | None = None,
    ) -> EmployeeRead:
        employee = await self._require_employee_locked(company_id, employee_id)
        current = employee.employment_status
        ensure_status_transition(current, target)

        if termination_date is not None:
            ensure_termination_date(employee.hire_date, termination_date)

        effective = effective_date or await self._today(company_id)
        previous = {
            "employment_status": str(current),
            "is_active": employee.is_active,
            "termination_date": _jsonable(employee.termination_date),
        }

        employee.employment_status = target
        employee.is_active = _active_flag(target)
        if target.is_terminal:
            employee.termination_date = termination_date or effective
        elif current.is_terminal:  # pragma: no cover - graph forbids it today
            employee.termination_date = None
        employee.updated_by = actor.user_id
        employee.version += 1

        new_data = {
            "employment_status": str(target),
            "is_active": employee.is_active,
            "termination_date": _jsonable(employee.termination_date),
        }
        event_type, audit_action = _STATUS_EVENTS[target]
        self._lifecycle(
            employee,
            event_type,
            effective,
            actor,
            previous_data=previous,
            new_data=new_data,
            reason=reason,
        )
        self._audit(audit_action, employee, actor, previous_data=previous, new_data=new_data)
        self.uow.outbox.enqueue(
            EmployeeStatusChanged(
                aggregate_id=employee.id,
                company_id=company_id,
                employee_number=employee.employee_number,
                previous_status=str(current),
                new_status=str(target),
                effective_date=effective,
                reason=reason,
                changed_by=actor.user_id,
            )
        )
        await self.uow.commit()

        metrics.employee_lifecycle_events_total.labels(str(company_id), str(event_type)).inc()
        return EmployeeRead.model_validate(employee)

    async def _today(self, company_id: UUID) -> date:
        """Today in the company's timezone, not the server's."""
        company = await self.uow.companies.get(company_id)
        return business_today(company.timezone if company else None)

    async def _require_employee(self, company_id: UUID, employee_id: UUID) -> Employee:
        row = await self.uow.employees.get_scoped(company_id, employee_id)
        if row is None:
            raise EmployeeNotFoundError(details={"employee_id": str(employee_id)})
        return row

    async def _require_employee_locked(self, company_id: UUID, employee_id: UUID) -> Employee:
        row = await self.uow.employees.get_for_update(company_id, employee_id)
        if row is None:
            raise EmployeeNotFoundError(details={"employee_id": str(employee_id)})
        return row

    async def _validate_manager(
        self, company_id: UUID, employee_id: UUID, manager_id: UUID
    ) -> None:
        manager = await self.uow.employees.get_scoped(company_id, manager_id)
        if manager is None:
            raise InvalidManagerError(
                "That manager does not exist in this company.",
                details={"manager_id": str(manager_id)},
            )
        if not manager.employment_status.is_employed:
            raise InvalidManagerError(
                "A departed employee cannot manage anybody.",
                details={"manager_id": str(manager_id), "status": str(manager.employment_status)},
            )
        chain = await self.uow.employees.manager_chain_ids(manager_id)
        ensure_no_manager_cycle(employee_id, manager_id, [manager_id, *chain])

    async def _validate_org_references(
        self,
        company_id: UUID,
        *,
        department_id: UUID | None = None,
        designation_id: UUID | None = None,
        location_id: UUID | None = None,
    ) -> None:
        from app.modules.employees.domain.exceptions import (
            DepartmentNotFoundError,
            DesignationNotFoundError,
            LocationNotFoundError,
        )

        if department_id is not None and (
            await self.uow.departments.get_scoped(company_id, department_id) is None
        ):
            raise DepartmentNotFoundError(details={"department_id": str(department_id)})
        if designation_id is not None and (
            await self.uow.designations.get_scoped(company_id, designation_id) is None
        ):
            raise DesignationNotFoundError(details={"designation_id": str(designation_id)})
        if location_id is not None and (
            await self.uow.locations.get_scoped(company_id, location_id) is None
        ):
            raise LocationNotFoundError(details={"location_id": str(location_id)})

    def _lifecycle(
        self,
        employee: Employee,
        event_type: LifecycleEventType,
        effective_date: date,
        actor: ActorContext,
        *,
        previous_data: dict[str, Any] | None = None,
        new_data: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> None:
        self.uow.lifecycle.add(
            EmployeeLifecycleEvent(
                company_id=employee.company_id,
                employee_id=employee.id,
                event_type=event_type,
                effective_date=effective_date,
                previous_data=previous_data or {},
                new_data=new_data or {},
                reason=reason,
                created_by=actor.user_id,
            )
        )

    def _audit(
        self,
        action: AuditAction,
        employee: Employee,
        actor: ActorContext,
        *,
        previous_data: dict[str, Any] | None = None,
        new_data: dict[str, Any] | None = None,
    ) -> None:
        self.uow.audit.record(
            action=action,
            entity_type="Employee",
            entity_id=employee.id,
            company_id=employee.company_id,
            user_id=actor.user_id,
            previous_data=previous_data,
            new_data=new_data,
            context={"employee_number": employee.employee_number},
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )

    @staticmethod
    def _snapshot(employee: Employee) -> dict[str, Any]:
        """Audit snapshot. Deliberately excludes personal contact details."""
        return {
            "employee_number": employee.employee_number,
            "full_name": employee.full_name,
            "work_email": employee.work_email,
            "hire_date": _jsonable(employee.hire_date),
            "employment_status": str(employee.employment_status),
            "department_id": _jsonable(employee.department_id),
            "designation_id": _jsonable(employee.designation_id),
            "manager_id": _jsonable(employee.manager_id),
            "location_id": _jsonable(employee.location_id),
        }


def _active_flag(status: EmploymentStatus) -> bool:
    """Keep `is_active` in step with `employment_status`.

    A CHECK constraint enforces the same relationship, so a mismatch here
    fails the transaction rather than silently corrupting headcount reports.
    """
    return status in (
        EmploymentStatus.ACTIVE,
        EmploymentStatus.PROBATION,
        EmploymentStatus.ON_LEAVE,
        EmploymentStatus.SUSPENDED,
    )


def _jsonable(value: object) -> object:
    from datetime import datetime as _dt

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, _dt | date):
        return value.isoformat()
    if hasattr(value, "value"):
        return str(value)
    return value


__all__ = ["EmployeeFilters", "EmployeeService"]
