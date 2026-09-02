"""Employment contract endpoints."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.application.contracts.schemas import (
    ContractCreate,
    ContractRead,
    ContractTerminateRequest,
    ContractUpdate,
)
from app.application.contracts.service import ContractService
from app.core.deps import ActorDep, UowDep, require
from app.domain.identity.permissions import Permission

router = APIRouter(prefix="/employment-contracts", tags=["contracts"])

CompanyId = Annotated[UUID, Query(description="The company that owns this resource.")]
ReadDep = Depends(require(Permission.EMPLOYEES_READ))
ManageDep = Depends(require(Permission.EMPLOYEES_MANAGE))


@router.post(
    "",
    response_model=ContractRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an employment contract",
    dependencies=[ManageDep],
)
async def create_contract(
    company_id: CompanyId, payload: ContractCreate, uow: UowDep, actor: ActorDep
) -> ContractRead:
    """Set `activate: true` to put it straight into force.

    An employee may hold at most one ACTIVE contract, enforced by a partial
    unique index — two simultaneous activations cannot both succeed.
    """
    return await ContractService(uow).create(company_id, payload, actor)


@router.get(
    "",
    response_model=list[ContractRead],
    summary="List an employee's contracts",
    dependencies=[ReadDep],
)
async def list_contracts(
    company_id: CompanyId, employee_id: UUID, uow: UowDep
) -> list[ContractRead]:
    """Status is reported as of today, so a contract past its end date reads
    as EXPIRED even if no batch job has run."""
    return await ContractService(uow).list_for_employee(company_id, employee_id)


@router.get(
    "/{contract_id}",
    response_model=ContractRead,
    summary="Fetch a contract",
    dependencies=[ReadDep],
)
async def get_contract(company_id: CompanyId, contract_id: UUID, uow: UowDep) -> ContractRead:
    return await ContractService(uow).get(company_id, contract_id)


@router.patch(
    "/{contract_id}",
    response_model=ContractRead,
    summary="Update a draft or active contract",
    dependencies=[ManageDep],
)
async def update_contract(
    company_id: CompanyId,
    contract_id: UUID,
    payload: ContractUpdate,
    uow: UowDep,
    actor: ActorDep,
) -> ContractRead:
    return await ContractService(uow).update(company_id, contract_id, payload, actor)


@router.post(
    "/{contract_id}/activate",
    response_model=ContractRead,
    summary="Put a draft contract into force",
    dependencies=[ManageDep],
)
async def activate_contract(
    company_id: CompanyId, contract_id: UUID, uow: UowDep, actor: ActorDep
) -> ContractRead:
    return await ContractService(uow).activate(company_id, contract_id, actor)


@router.post(
    "/{contract_id}/terminate",
    response_model=ContractRead,
    summary="Terminate a contract",
    dependencies=[ManageDep],
)
async def terminate_contract(
    company_id: CompanyId,
    contract_id: UUID,
    payload: ContractTerminateRequest,
    uow: UowDep,
    actor: ActorDep,
) -> ContractRead:
    return await ContractService(uow).terminate(company_id, contract_id, payload, actor)
