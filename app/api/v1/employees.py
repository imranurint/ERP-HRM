"""Employee and lifecycle endpoints."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.application.employees.schemas import (
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
from app.application.employees.service import EmployeeFilters, EmployeeService
from app.core.deps import (
    ActorDep,
    CurrentUser,
    UowDep,
    ensure_self_or_permission,
    require,
)
from app.core.pagination import Page, PageParams, page_params
from app.domain.employees.enums import EmploymentStatus
from app.domain.identity.permissions import Permission

router = APIRouter(prefix="/employees", tags=["employees"])

CompanyId = Annotated[UUID, Query(description="The company that owns this resource.")]
ReadDep = Depends(require(Permission.EMPLOYEES_READ))
WriteDep = Depends(require(Permission.EMPLOYEES_WRITE))
ManageDep = Depends(require(Permission.EMPLOYEES_MANAGE))


@router.post(
    "",
    response_model=EmployeeRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an employee",
    dependencies=[WriteDep],
)
async def create_employee(
    company_id: CompanyId, payload: EmployeeCreate, uow: UowDep, actor: ActorDep
) -> EmployeeRead:
    """Omit `employee_number` to have one allocated atomically.

    Allocation uses a single `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`,
    never `SELECT MAX()`, so two simultaneous hires cannot collide.
    """
    return await EmployeeService(uow).create(company_id, payload, actor)


@router.get(
    "",
    response_model=Page[EmployeeRead],
    summary="List employees",
    dependencies=[ReadDep],
)
async def list_employees(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    department_id: Annotated[UUID | None, Query()] = None,
    designation_id: Annotated[UUID | None, Query()] = None,
    location_id: Annotated[UUID | None, Query()] = None,
    manager_id: Annotated[UUID | None, Query()] = None,
    employment_status: Annotated[EmploymentStatus | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[EmployeeRead]:
    return await EmployeeService(uow).list_page(
        company_id,
        params,
        EmployeeFilters(
            department_id=department_id,
            designation_id=designation_id,
            location_id=location_id,
            manager_id=manager_id,
            employment_status=employment_status,
            is_active=is_active,
            search=search,
        ),
    )


@router.get("/{employee_id}", response_model=EmployeeRead, summary="Fetch an employee")
async def get_employee(
    company_id: CompanyId, employee_id: UUID, uow: UowDep, user: CurrentUser
) -> EmployeeRead:
    """An employee may always read their own record; reading anybody else's
    requires `employees:read`."""
    ensure_self_or_permission(user, employee_id, Permission.EMPLOYEES_READ)
    return await EmployeeService(uow).get(company_id, employee_id)


@router.patch(
    "/{employee_id}",
    response_model=EmployeeRead,
    summary="Update employee contact and identity details",
    dependencies=[WriteDep],
)
async def update_employee(
    company_id: CompanyId,
    employee_id: UUID,
    payload: EmployeeUpdate,
    uow: UowDep,
    actor: ActorDep,
) -> EmployeeRead:
    """Contact and identity fields only.

    Department, designation and manager changes go through `/transfer` and
    `/promote`, and employment status through the lifecycle routes, so every
    material change leaves a lifecycle event behind. Send `version` to take an
    optimistic lock.
    """
    return await EmployeeService(uow).update(company_id, employee_id, payload, actor)


@router.get(
    "/{employee_id}/lifecycle",
    response_model=Page[LifecycleEventRead],
    summary="An employee's lifecycle history",
    dependencies=[ReadDep],
)
async def employee_lifecycle(
    company_id: CompanyId,
    employee_id: UUID,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[LifecycleEventRead]:
    return await EmployeeService(uow).lifecycle_history(company_id, employee_id, params)


# ----------------------------------------------------------------- lifecycle
@router.post(
    "/{employee_id}/activate",
    response_model=EmployeeRead,
    summary="Move an employee to ACTIVE",
    dependencies=[ManageDep],
)
async def activate_employee(
    company_id: CompanyId,
    employee_id: UUID,
    payload: ActivateRequest,
    uow: UowDep,
    actor: ActorDep,
) -> EmployeeRead:
    """Confirms out of probation, or returns from leave or suspension."""
    return await EmployeeService(uow).activate(company_id, employee_id, payload, actor)


@router.post(
    "/{employee_id}/suspend",
    response_model=EmployeeRead,
    summary="Suspend an employee",
    dependencies=[ManageDep],
)
async def suspend_employee(
    company_id: CompanyId,
    employee_id: UUID,
    payload: SuspendRequest,
    uow: UowDep,
    actor: ActorDep,
) -> EmployeeRead:
    return await EmployeeService(uow).suspend(company_id, employee_id, payload, actor)


@router.post(
    "/{employee_id}/terminate",
    response_model=EmployeeRead,
    summary="End employment",
    dependencies=[ManageDep],
)
async def terminate_employee(
    company_id: CompanyId,
    employee_id: UUID,
    payload: TerminateRequest,
    uow: UowDep,
    actor: ActorDep,
) -> EmployeeRead:
    """The employee row is never deleted.

    Set `resigned: true` when the employee left of their own accord —
    TERMINATED and RESIGNED are legally different and reports separate them.
    Both are terminal: a returning employee is a rehire with a fresh record,
    which keeps tenure and leave accrual honest.
    """
    return await EmployeeService(uow).terminate(company_id, employee_id, payload, actor)


@router.post(
    "/{employee_id}/transfer",
    response_model=EmployeeRead,
    summary="Transfer department, location or manager",
    dependencies=[ManageDep],
)
async def transfer_employee(
    company_id: CompanyId,
    employee_id: UUID,
    payload: TransferRequest,
    uow: UowDep,
    actor: ActorDep,
) -> EmployeeRead:
    """A new manager is cycle-checked against the whole reporting line."""
    return await EmployeeService(uow).transfer(company_id, employee_id, payload, actor)


@router.post(
    "/{employee_id}/promote",
    response_model=EmployeeRead,
    summary="Change designation",
    dependencies=[ManageDep],
)
async def promote_employee(
    company_id: CompanyId,
    employee_id: UUID,
    payload: PromoteRequest,
    uow: UowDep,
    actor: ActorDep,
) -> EmployeeRead:
    return await EmployeeService(uow).promote(company_id, employee_id, payload, actor)
