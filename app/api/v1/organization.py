"""Department, designation, location and holiday endpoints.

`company_id` is an explicit query parameter on every route. Permissions in
this system are global, so the company is the isolation boundary and the
service asserts it on every id it resolves.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

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
from app.application.organization.service import OrganizationService
from app.core.deps import ActorDep, UowDep, require
from app.core.pagination import Page, PageParams, page_params
from app.domain.identity.permissions import Permission

router = APIRouter(tags=["organization"])

CompanyId = Annotated[UUID, Query(description="The company that owns this resource.")]
ReadDep = Depends(require(Permission.EMPLOYEES_READ))
ManageDep = Depends(require(Permission.ORGANIZATION_MANAGE))


# ----------------------------------------------------------------- departments
@router.post(
    "/departments",
    response_model=DepartmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a department",
    dependencies=[ManageDep],
)
async def create_department(
    company_id: CompanyId, payload: DepartmentCreate, uow: UowDep, actor: ActorDep
) -> DepartmentRead:
    return await OrganizationService(uow).create_department(company_id, payload, actor)


@router.get(
    "/departments",
    response_model=Page[DepartmentRead],
    summary="List departments",
    dependencies=[ReadDep],
)
async def list_departments(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[DepartmentRead]:
    return await OrganizationService(uow).list_departments(company_id, params, is_active=is_active)


@router.get(
    "/departments/{department_id}",
    response_model=DepartmentRead,
    summary="Fetch a department",
    dependencies=[ReadDep],
)
async def get_department(company_id: CompanyId, department_id: UUID, uow: UowDep) -> DepartmentRead:
    return await OrganizationService(uow).get_department(company_id, department_id)


@router.patch(
    "/departments/{department_id}",
    response_model=DepartmentRead,
    summary="Update a department",
    dependencies=[ManageDep],
)
async def update_department(
    company_id: CompanyId,
    department_id: UUID,
    payload: DepartmentUpdate,
    uow: UowDep,
    actor: ActorDep,
) -> DepartmentRead:
    """Re-parenting is cycle-checked: a department cannot become its own
    ancestor, or every org-chart traversal after it would never terminate."""
    return await OrganizationService(uow).update_department(
        company_id, department_id, payload, actor
    )


# ---------------------------------------------------------------- designations
@router.post(
    "/designations",
    response_model=DesignationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a designation",
    dependencies=[ManageDep],
)
async def create_designation(
    company_id: CompanyId, payload: DesignationCreate, uow: UowDep, actor: ActorDep
) -> DesignationRead:
    return await OrganizationService(uow).create_designation(company_id, payload, actor)


@router.get(
    "/designations",
    response_model=Page[DesignationRead],
    summary="List designations",
    dependencies=[ReadDep],
)
async def list_designations(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[DesignationRead]:
    return await OrganizationService(uow).list_designations(company_id, params, is_active=is_active)


@router.patch(
    "/designations/{designation_id}",
    response_model=DesignationRead,
    summary="Update a designation",
    dependencies=[ManageDep],
)
async def update_designation(
    company_id: CompanyId,
    designation_id: UUID,
    payload: DesignationUpdate,
    uow: UowDep,
    actor: ActorDep,
) -> DesignationRead:
    return await OrganizationService(uow).update_designation(
        company_id, designation_id, payload, actor
    )


# ------------------------------------------------------------------- locations
@router.post(
    "/locations",
    response_model=LocationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a location",
    dependencies=[ManageDep],
)
async def create_location(
    company_id: CompanyId, payload: LocationCreate, uow: UowDep, actor: ActorDep
) -> LocationRead:
    """A location may carry its own timezone, overriding the company's. A
    multi-country company needs that, or one office's attendance lands on the
    wrong calendar day."""
    return await OrganizationService(uow).create_location(company_id, payload, actor)


@router.get(
    "/locations",
    response_model=Page[LocationRead],
    summary="List locations",
    dependencies=[ReadDep],
)
async def list_locations(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[LocationRead]:
    return await OrganizationService(uow).list_locations(company_id, params, is_active=is_active)


@router.patch(
    "/locations/{location_id}",
    response_model=LocationRead,
    summary="Update a location",
    dependencies=[ManageDep],
)
async def update_location(
    company_id: CompanyId,
    location_id: UUID,
    payload: LocationUpdate,
    uow: UowDep,
    actor: ActorDep,
) -> LocationRead:
    return await OrganizationService(uow).update_location(company_id, location_id, payload, actor)


# -------------------------------------------------------------------- holidays
@router.post(
    "/holidays",
    response_model=HolidayRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a holiday",
    dependencies=[ManageDep],
)
async def create_holiday(
    company_id: CompanyId, payload: HolidayCreate, uow: UowDep, actor: ActorDep
) -> HolidayRead:
    """Holidays are load-bearing, not decoration: they are excluded from leave
    day counts and from payroll working days."""
    return await OrganizationService(uow).create_holiday(company_id, payload, actor)


@router.get(
    "/holidays",
    response_model=list[HolidayRead],
    summary="List holidays",
    dependencies=[ReadDep],
)
async def list_holidays(
    company_id: CompanyId,
    uow: UowDep,
    year: Annotated[int | None, Query(ge=1900, le=9999)] = None,
) -> list[HolidayRead]:
    return await OrganizationService(uow).list_holidays(company_id, year=year)
