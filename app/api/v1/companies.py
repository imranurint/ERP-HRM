"""Company endpoints."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.application.companies.schemas import (
    CompanyCreate,
    CompanyRead,
    CompanyUpdate,
    CurrencyRead,
)
from app.application.companies.service import CompanyService
from app.core.deps import ActorDep, UowDep, require
from app.core.pagination import Page, PageParams, page_params
from app.domain.identity.permissions import Permission

router = APIRouter(prefix="/companies", tags=["companies"])


@router.post(
    "",
    response_model=CompanyRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a company",
    dependencies=[Depends(require(Permission.ORGANIZATION_MANAGE))],
)
async def create_company(payload: CompanyCreate, uow: UowDep, actor: ActorDep) -> CompanyRead:
    return await CompanyService(uow).create(payload, actor)


@router.get(
    "",
    response_model=Page[CompanyRead],
    summary="List companies",
    dependencies=[Depends(require(Permission.EMPLOYEES_READ))],
)
async def list_companies(
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[CompanyRead]:
    return await CompanyService(uow).list_page(params, is_active=is_active)


@router.get(
    "/currencies",
    response_model=list[CurrencyRead],
    summary="List active currencies",
    dependencies=[Depends(require(Permission.EMPLOYEES_READ))],
)
async def list_currencies(uow: UowDep) -> list[CurrencyRead]:
    return await CompanyService(uow).list_currencies()


@router.get(
    "/{company_id}",
    response_model=CompanyRead,
    summary="Fetch a company",
    dependencies=[Depends(require(Permission.EMPLOYEES_READ))],
)
async def get_company(company_id: UUID, uow: UowDep) -> CompanyRead:
    return await CompanyService(uow).get(company_id)


@router.patch(
    "/{company_id}",
    response_model=CompanyRead,
    summary="Update a company",
    dependencies=[Depends(require(Permission.ORGANIZATION_MANAGE))],
)
async def update_company(
    company_id: UUID, payload: CompanyUpdate, uow: UowDep, actor: ActorDep
) -> CompanyRead:
    """`base_currency_code` is deliberately not updatable: changing it would
    reinterpret every salary structure and payslip the company has ever
    issued."""
    return await CompanyService(uow).update(company_id, payload, actor)
