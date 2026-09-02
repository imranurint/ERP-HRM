"""Salary component and salary structure endpoints.

Reading salary is gated behind `payroll:read`, not `employees:read`: an HR
Officer who can maintain contact details has no business seeing what everybody
earns.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.application.salary.schemas import (
    FormulaRead,
    SalaryComponentCreate,
    SalaryComponentRead,
    SalaryComponentUpdate,
    SalaryStructureCreate,
    SalaryStructureRead,
)
from app.application.salary.service import SalaryService
from app.core.dates import business_today
from app.core.deps import ActorDep, UowDep, require
from app.core.pagination import Page, PageParams, page_params
from app.domain.identity.permissions import Permission

router = APIRouter(tags=["salary"])

CompanyId = Annotated[UUID, Query(description="The company that owns this resource.")]
ReadDep = Depends(require(Permission.PAYROLL_READ))
ManageDep = Depends(require(Permission.ORGANIZATION_MANAGE))


# ------------------------------------------------------------------ components
@router.post(
    "/salary-components",
    response_model=SalaryComponentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a salary component",
    dependencies=[ManageDep],
)
async def create_component(
    company_id: CompanyId, payload: SalaryComponentCreate, uow: UowDep, actor: ActorDep
) -> SalaryComponentRead:
    """`sequence` controls evaluation order.

    Components are applied lowest-first and each sees the running totals of
    everything before it, which is what makes "5% of gross" unambiguous.
    """
    return await SalaryService(uow).create_component(company_id, payload, actor)


@router.get(
    "/salary-components",
    response_model=Page[SalaryComponentRead],
    summary="List salary components",
    dependencies=[ReadDep],
)
async def list_components(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[SalaryComponentRead]:
    return await SalaryService(uow).list_components(company_id, params, is_active=is_active)


@router.patch(
    "/salary-components/{component_id}",
    response_model=SalaryComponentRead,
    summary="Update a salary component",
    dependencies=[ManageDep],
)
async def update_component(
    company_id: CompanyId,
    component_id: UUID,
    payload: SalaryComponentUpdate,
    uow: UowDep,
    actor: ActorDep,
) -> SalaryComponentRead:
    """`component_type` and `calculation_type` are deliberately not updatable:
    changing either would silently reinterpret every structure using it."""
    return await SalaryService(uow).update_component(company_id, component_id, payload, actor)


@router.get(
    "/salary-components/formulas",
    response_model=list[FormulaRead],
    summary="List registered payroll formulas",
    dependencies=[ReadDep],
)
async def list_formulas() -> list[FormulaRead]:
    """FORMULA components name a calculator registered in code, never an
    expression string. This is the catalogue of valid keys."""
    return SalaryService.list_formulas()


# ------------------------------------------------------------------ structures
@router.post(
    "/salary-structures",
    response_model=SalaryStructureRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a salary structure",
    dependencies=[ManageDep],
)
async def create_structure(
    company_id: CompanyId, payload: SalaryStructureCreate, uow: UowDep, actor: ActorDep
) -> SalaryStructureRead:
    """Activating writes a lifecycle event and a `SalaryChanged` domain event.

    An employee's structures may not overlap in time — enforced by a GiST
    exclusion constraint, so "what was their salary on date X?" has exactly
    one answer.
    """
    return await SalaryService(uow).create_structure(company_id, payload, actor)


@router.get(
    "/salary-structures",
    response_model=list[SalaryStructureRead],
    summary="List an employee's salary structures",
    dependencies=[ReadDep],
)
async def list_structures(
    company_id: CompanyId, employee_id: UUID, uow: UowDep
) -> list[SalaryStructureRead]:
    return await SalaryService(uow).list_structures_for_employee(company_id, employee_id)


@router.get(
    "/salary-structures/current",
    response_model=SalaryStructureRead,
    summary="The structure in effect on a date",
    dependencies=[ReadDep],
)
async def current_structure(
    company_id: CompanyId,
    employee_id: UUID,
    uow: UowDep,
    as_of: Annotated[date | None, Query()] = None,
) -> SalaryStructureRead:
    return await SalaryService(uow).current_structure(
        company_id, employee_id, as_of or business_today()
    )


@router.get(
    "/salary-structures/{structure_id}",
    response_model=SalaryStructureRead,
    summary="Fetch a salary structure",
    dependencies=[ReadDep],
)
async def get_structure(
    company_id: CompanyId, structure_id: UUID, uow: UowDep
) -> SalaryStructureRead:
    return await SalaryService(uow).get_structure(company_id, structure_id)


@router.post(
    "/salary-structures/{structure_id}/activate",
    response_model=SalaryStructureRead,
    summary="Activate a draft salary structure",
    dependencies=[ManageDep],
)
async def activate_structure(
    company_id: CompanyId, structure_id: UUID, uow: UowDep, actor: ActorDep
) -> SalaryStructureRead:
    return await SalaryService(uow).activate_structure(company_id, structure_id, actor)
