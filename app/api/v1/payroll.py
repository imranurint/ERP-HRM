"""Payroll endpoints.

The four payroll permissions are separate on purpose. A deployment can put
`payroll:calculate`, `payroll:approve` and `payroll:pay` in three different
hands, which is what makes approval a control rather than a formality.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.application.payroll.schemas import (
    PayrollApprovalPreview,
    PayrollCalculateRequest,
    PayrollPeriodCreate,
    PayrollPeriodRead,
    PayrollRunCreate,
    PayrollRunRead,
    PayrollSummaryRow,
    PayslipRead,
    VoidPayslipRequest,
)
from app.application.payroll.service import PayrollService
from app.core.deps import (
    ActorDep,
    CurrentUser,
    UowDep,
    ensure_self_or_permission,
    require,
)
from app.core.pagination import Page, PageParams, page_params
from app.domain.identity.permissions import Permission
from app.domain.payroll.enums import PayrollPeriodStatus, PayrollRunStatus, PayslipStatus

router = APIRouter(tags=["payroll"])

CompanyId = Annotated[UUID, Query(description="The company that owns this resource.")]
ReadDep = Depends(require(Permission.PAYROLL_READ))
CalculateDep = Depends(require(Permission.PAYROLL_CALCULATE))
ApproveDep = Depends(require(Permission.PAYROLL_APPROVE))
PayDep = Depends(require(Permission.PAYROLL_PAY))


# -------------------------------------------------------------------- periods
@router.post(
    "/payroll-periods",
    response_model=PayrollPeriodRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a payroll period",
    dependencies=[CalculateDep],
)
async def create_period(
    company_id: CompanyId, payload: PayrollPeriodCreate, uow: UowDep, actor: ActorDep
) -> PayrollPeriodRead:
    """Golden Rule 1: periods for one company cannot overlap.

    Enforced by a GiST exclusion constraint, not only by an application check
    — two simultaneous inserts would both pass the check.
    """
    return await PayrollService(uow).create_period(company_id, payload, actor)


@router.get(
    "/payroll-periods",
    response_model=Page[PayrollPeriodRead],
    summary="List payroll periods",
    dependencies=[ReadDep],
)
async def list_periods(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    period_status: Annotated[PayrollPeriodStatus | None, Query()] = None,
) -> Page[PayrollPeriodRead]:
    return await PayrollService(uow).list_periods(company_id, params, status=period_status)


@router.get(
    "/payroll-periods/{period_id}",
    response_model=PayrollPeriodRead,
    summary="Fetch a payroll period",
    dependencies=[ReadDep],
)
async def get_period(company_id: CompanyId, period_id: UUID, uow: UowDep) -> PayrollPeriodRead:
    return await PayrollService(uow).get_period(company_id, period_id)


@router.post(
    "/payroll-periods/{period_id}/close",
    response_model=PayrollPeriodRead,
    summary="Close a payroll period",
    dependencies=[ApproveDep],
)
async def close_period(
    company_id: CompanyId, period_id: UUID, uow: UowDep, actor: ActorDep
) -> PayrollPeriodRead:
    """A closed period accepts no further runs, and the attendance and leave
    underneath it are frozen."""
    return await PayrollService(uow).close_period(company_id, period_id, actor)


# ----------------------------------------------------------------------- runs
@router.post(
    "/payroll-runs",
    response_model=PayrollRunRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a payroll run",
    dependencies=[CalculateDep],
)
async def create_run(
    company_id: CompanyId, payload: PayrollRunCreate, uow: UowDep, actor: ActorDep
) -> PayrollRunRead:
    """Set `adjusts_run_id` when correcting an already-approved run.

    Golden Rule 7 forbids editing approved payroll, so a correction is a
    *new* run alongside the original and both stay visible.
    """
    return await PayrollService(uow).create_run(company_id, payload, actor)


@router.get(
    "/payroll-runs",
    response_model=Page[PayrollRunRead],
    summary="List payroll runs",
    dependencies=[ReadDep],
)
async def list_runs(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    period_id: Annotated[UUID | None, Query()] = None,
    run_status: Annotated[PayrollRunStatus | None, Query()] = None,
) -> Page[PayrollRunRead]:
    return await PayrollService(uow).list_runs(
        company_id, params, period_id=period_id, status=run_status
    )


@router.get(
    "/payroll-runs/{run_id}",
    response_model=PayrollRunRead,
    summary="Fetch a payroll run",
    dependencies=[ReadDep],
)
async def get_run(company_id: CompanyId, run_id: UUID, uow: UowDep) -> PayrollRunRead:
    return await PayrollService(uow).get_run(company_id, run_id)


@router.post(
    "/payroll-runs/{run_id}/calculate",
    response_model=PayrollRunRead,
    summary="Calculate every payslip in the run",
    dependencies=[CalculateDep],
)
async def calculate_run(
    company_id: CompanyId,
    run_id: UUID,
    payload: PayrollCalculateRequest,
    uow: UowDep,
    actor: ActorDep,
) -> PayrollRunRead:
    """One transaction: either every payslip lands or none does.

    Recalculating discards the previous payslips and computes from scratch,
    which is safe because the calculation is deterministic and because an
    approved or paid run is refused outright.
    """
    return await PayrollService(uow).calculate(company_id, run_id, payload, actor)


@router.get(
    "/payroll-runs/{run_id}/journal-preview",
    response_model=PayrollApprovalPreview,
    summary="The accounting journal approval would produce",
    dependencies=[ReadDep],
)
async def journal_preview(
    company_id: CompanyId, run_id: UUID, uow: UowDep
) -> PayrollApprovalPreview:
    """Read-only. Lets somebody reconcile the accounting effect before
    committing to it. Debits and credits balance by construction."""
    return await PayrollService(uow).approval_preview(company_id, run_id)


@router.get(
    "/payroll-runs/{run_id}/summary",
    response_model=list[PayrollSummaryRow],
    summary="Per-employee totals for a run",
    dependencies=[ReadDep],
)
async def run_summary(company_id: CompanyId, run_id: UUID, uow: UowDep) -> list[PayrollSummaryRow]:
    return await PayrollService(uow).run_summary(company_id, run_id)


@router.post(
    "/payroll-runs/{run_id}/approve",
    response_model=PayrollRunRead,
    summary="Approve a calculated payroll run",
    dependencies=[ApproveDep],
)
async def approve_run(
    company_id: CompanyId, run_id: UUID, uow: UowDep, actor: ActorDep
) -> PayrollRunRead:
    """After this the numbers are immutable (Golden Rules 2 and 3).

    Emits a `PayrollApproved` event carrying a journal-ready payload for the
    Accounting module.
    """
    return await PayrollService(uow).approve(company_id, run_id, actor)


@router.post(
    "/payroll-runs/{run_id}/pay",
    response_model=PayrollRunRead,
    summary="Mark an approved run as paid",
    dependencies=[PayDep],
)
async def pay_run(
    company_id: CompanyId, run_id: UUID, uow: UowDep, actor: ActorDep
) -> PayrollRunRead:
    return await PayrollService(uow).pay(company_id, run_id, actor)


# ------------------------------------------------------------------- payslips
@router.get(
    "/payslips",
    response_model=Page[PayslipRead],
    summary="List payslips",
    dependencies=[ReadDep],
)
async def list_payslips(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    employee_id: Annotated[UUID | None, Query()] = None,
    run_id: Annotated[UUID | None, Query()] = None,
    payslip_status: Annotated[PayslipStatus | None, Query()] = None,
) -> Page[PayslipRead]:
    return await PayrollService(uow).list_payslips(
        company_id, params, employee_id=employee_id, run_id=run_id, status=payslip_status
    )


@router.get("/payslips/{payslip_id}", response_model=PayslipRead, summary="Fetch a payslip")
async def get_payslip(
    company_id: CompanyId, payslip_id: UUID, uow: UowDep, user: CurrentUser
) -> PayslipRead:
    """An employee may always read their own payslip; anybody else's requires
    `payroll:read`."""
    payslip = await PayrollService(uow).get_payslip(company_id, payslip_id)
    ensure_self_or_permission(user, payslip.employee_id, Permission.PAYROLL_READ)
    return payslip


@router.post(
    "/payslips/{payslip_id}/void",
    response_model=PayslipRead,
    summary="Void a payslip",
    dependencies=[ApproveDep],
)
async def void_payslip(
    company_id: CompanyId,
    payslip_id: UUID,
    payload: VoidPayslipRequest,
    uow: UowDep,
    actor: ActorDep,
) -> PayslipRead:
    """Excludes a payslip from its run without deleting it.

    Refused once paid: money has left the building, and the correction is an
    adjustment run that leaves both entries visible.
    """
    return await PayrollService(uow).void_payslip(company_id, payslip_id, payload, actor)
