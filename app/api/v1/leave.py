"""Leave endpoints."""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.application.leave.schemas import (
    AssignPolicyRequest,
    BalanceAdjustmentRequest,
    EmployeeLeavePolicyRead,
    LeaveBalanceRead,
    LeavePolicyCreate,
    LeavePolicyRead,
    LeaveRejectRequest,
    LeaveRequestCreate,
    LeaveRequestRead,
    LeaveTransactionRead,
    LeaveTypeCreate,
    LeaveTypeRead,
    LeaveTypeUpdate,
)
from app.application.leave.service import LeaveService
from app.core.dates import business_today
from app.core.deps import (
    ActorDep,
    CurrentUser,
    UowDep,
    ensure_self_or_permission,
    require,
)
from app.core.pagination import Page, PageParams, page_params
from app.domain.identity.exceptions import NotYourRecordError
from app.domain.identity.permissions import Permission
from app.domain.leave.enums import LeaveRequestStatus

router = APIRouter(tags=["leave"])

CompanyId = Annotated[UUID, Query(description="The company that owns this resource.")]
ReadDep = Depends(require(Permission.LEAVE_READ))
ApproveDep = Depends(require(Permission.LEAVE_APPROVE))
ManageDep = Depends(require(Permission.LEAVE_MANAGE))


# ---------------------------------------------------------------- leave types
@router.post(
    "/leave-types",
    response_model=LeaveTypeRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a leave type",
    dependencies=[ManageDep],
)
async def create_leave_type(
    company_id: CompanyId, payload: LeaveTypeCreate, uow: UowDep, actor: ActorDep
) -> LeaveTypeRead:
    """`is_paid` is what payroll reads: unpaid leave reduces payable days,
    paid leave does not."""
    return await LeaveService(uow).create_type(company_id, payload, actor)


@router.get(
    "/leave-types",
    response_model=Page[LeaveTypeRead],
    summary="List leave types",
    dependencies=[ReadDep],
)
async def list_leave_types(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[LeaveTypeRead]:
    return await LeaveService(uow).list_types(company_id, params, is_active=is_active)


@router.patch(
    "/leave-types/{leave_type_id}",
    response_model=LeaveTypeRead,
    summary="Update a leave type",
    dependencies=[ManageDep],
)
async def update_leave_type(
    company_id: CompanyId,
    leave_type_id: UUID,
    payload: LeaveTypeUpdate,
    uow: UowDep,
    actor: ActorDep,
) -> LeaveTypeRead:
    return await LeaveService(uow).update_type(company_id, leave_type_id, payload, actor)


# ------------------------------------------------------------------ policies
@router.post(
    "/leave-policies",
    response_model=LeavePolicyRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a leave policy",
    dependencies=[ManageDep],
)
async def create_policy(
    company_id: CompanyId, payload: LeavePolicyCreate, uow: UowDep, actor: ActorDep
) -> LeavePolicyRead:
    return await LeaveService(uow).create_policy(company_id, payload, actor)


@router.get(
    "/leave-policies",
    response_model=Page[LeavePolicyRead],
    summary="List leave policies",
    dependencies=[ReadDep],
)
async def list_policies(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[LeavePolicyRead]:
    return await LeaveService(uow).list_policies(company_id, params)


@router.post(
    "/leave-policies/assignments",
    response_model=EmployeeLeavePolicyRead,
    status_code=status.HTTP_201_CREATED,
    summary="Assign a leave policy to an employee",
    dependencies=[ManageDep],
)
async def assign_policy(
    company_id: CompanyId, payload: AssignPolicyRequest, uow: UowDep, actor: ActorDep
) -> EmployeeLeavePolicyRead:
    """With `grant_entitlement`, the policy's annual days are credited as
    ENTITLEMENT transactions, subject to each rule's minimum service."""
    return await LeaveService(uow).assign_policy(company_id, payload, actor)


# ------------------------------------------------------------------ requests
@router.post(
    "/leave-requests",
    response_model=LeaveRequestRead,
    status_code=status.HTTP_201_CREATED,
    summary="Request leave",
    dependencies=[ReadDep],
)
async def create_leave_request(
    company_id: CompanyId,
    payload: LeaveRequestCreate,
    uow: UowDep,
    user: CurrentUser,
    actor: ActorDep,
) -> LeaveRequestRead:
    """Omit `employee_id` to book your own leave.

    Day counting excludes weekends and holidays unless the leave type says
    otherwise. A leave type with `requires_approval: false` is approved on
    submission — otherwise it would sit PENDING with nobody to action it.
    """
    employee_id = _resolve_employee(user, payload.employee_id, Permission.LEAVE_MANAGE)
    return await LeaveService(uow).create_request(company_id, payload, employee_id, actor)


@router.get(
    "/leave-requests",
    response_model=Page[LeaveRequestRead],
    summary="List leave requests",
    dependencies=[ReadDep],
)
async def list_leave_requests(
    company_id: CompanyId,
    uow: UowDep,
    user: CurrentUser,
    params: Annotated[PageParams, Depends(page_params)],
    employee_id: Annotated[UUID | None, Query()] = None,
    leave_type_id: Annotated[UUID | None, Query()] = None,
    leave_status: Annotated[LeaveRequestStatus | None, Query()] = None,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
) -> Page[LeaveRequestRead]:
    """Without `leave:approve` or `leave:manage`, this is scoped to yourself."""
    scoped = _scope_listing(user, employee_id)
    return await LeaveService(uow).list_requests(
        company_id,
        params,
        employee_id=scoped,
        leave_type_id=leave_type_id,
        status=leave_status,
        start_date=start_date,
        end_date=end_date,
    )


@router.get(
    "/leave-requests/{request_id}",
    response_model=LeaveRequestRead,
    summary="Fetch a leave request",
    dependencies=[ReadDep],
)
async def get_leave_request(
    company_id: CompanyId, request_id: UUID, uow: UowDep
) -> LeaveRequestRead:
    return await LeaveService(uow).get_request(company_id, request_id)


@router.post(
    "/leave-requests/{request_id}/submit",
    response_model=LeaveRequestRead,
    summary="Submit a draft leave request",
    dependencies=[ReadDep],
)
async def submit_leave_request(
    company_id: CompanyId, request_id: UUID, uow: UowDep, actor: ActorDep
) -> LeaveRequestRead:
    return await LeaveService(uow).submit_request(company_id, request_id, actor)


@router.post(
    "/leave-requests/{request_id}/approve",
    response_model=LeaveRequestRead,
    summary="Approve a leave request",
    dependencies=[ApproveDep],
)
async def approve_leave_request(
    company_id: CompanyId, request_id: UUID, uow: UowDep, actor: ActorDep
) -> LeaveRequestRead:
    """Nobody approves their own leave, whatever permissions they hold.

    The request row is locked for the transaction, so two approvers cannot
    both consume the balance.
    """
    return await LeaveService(uow).approve(company_id, request_id, actor)


@router.post(
    "/leave-requests/{request_id}/reject",
    response_model=LeaveRequestRead,
    summary="Reject a leave request",
    dependencies=[ApproveDep],
)
async def reject_leave_request(
    company_id: CompanyId,
    request_id: UUID,
    payload: LeaveRejectRequest,
    uow: UowDep,
    actor: ActorDep,
) -> LeaveRequestRead:
    """No balance movement: a pending request never consumed any."""
    return await LeaveService(uow).reject(company_id, request_id, payload.rejection_reason, actor)


@router.post(
    "/leave-requests/{request_id}/cancel",
    response_model=LeaveRequestRead,
    summary="Cancel a leave request",
    dependencies=[ReadDep],
)
async def cancel_leave_request(
    company_id: CompanyId, request_id: UUID, uow: UowDep, actor: ActorDep
) -> LeaveRequestRead:
    """Approved leave that has already started cannot be cancelled — record a
    balance adjustment instead, which leaves an audit trail."""
    return await LeaveService(uow).cancel(company_id, request_id, actor)


# ------------------------------------------------------------------ balances
@router.get(
    "/leave-balances",
    response_model=list[LeaveBalanceRead],
    summary="An employee's leave balances",
    dependencies=[ReadDep],
)
async def leave_balances(
    company_id: CompanyId,
    uow: UowDep,
    user: CurrentUser,
    employee_id: Annotated[UUID | None, Query()] = None,
    year: Annotated[int | None, Query(ge=1900, le=9999)] = None,
) -> list[LeaveBalanceRead]:
    """`available` is the closing balance minus days held by pending requests,
    which is what stops two same-morning requests both being approved against
    one remaining day."""
    target = _resolve_employee(user, employee_id, Permission.LEAVE_READ)
    return await LeaveService(uow).balances_for_employee(
        company_id, target, year or business_today().year
    )


@router.post(
    "/leave-balances/adjustments",
    response_model=LeaveBalanceRead,
    summary="Adjust a leave balance",
    dependencies=[ManageDep],
)
async def adjust_balance(
    company_id: CompanyId, payload: BalanceAdjustmentRequest, uow: UowDep, actor: ActorDep
) -> LeaveBalanceRead:
    """Always requires a written reason. Positive grants, negative withdraws."""
    return await LeaveService(uow).adjust_balance(company_id, payload, actor)


@router.get(
    "/leave-balances/transactions",
    response_model=list[LeaveTransactionRead],
    summary="The movements a balance is derived from",
    dependencies=[ReadDep],
)
async def leave_transactions(
    company_id: CompanyId,
    employee_id: UUID,
    leave_type_id: UUID,
    uow: UowDep,
    year: Annotated[int | None, Query(ge=1900, le=9999)] = None,
) -> list[LeaveTransactionRead]:
    """Append-only. Replaying these reproduces the balance exactly — that is
    what `make rebuild-leave-balances` verifies."""
    return await LeaveService(uow).list_transactions(
        company_id, employee_id, leave_type_id, year or business_today().year
    )


def _resolve_employee(user: CurrentUser, requested: UUID | None, permission: Permission) -> UUID:
    if requested is None:
        if user.employee_id is None:
            raise NotYourRecordError(
                "Your login is not linked to an employee record; specify employee_id explicitly.",
                details={"user_id": str(user.id)},
            )
        return user.employee_id
    ensure_self_or_permission(user, requested, permission)
    return requested


def _scope_listing(user: CurrentUser, requested: UUID | None) -> UUID | None:
    """Narrow a listing to the caller unless they can see everybody's."""
    if user.has_any(Permission.LEAVE_APPROVE, Permission.LEAVE_MANAGE):
        return requested
    if requested is not None:
        ensure_self_or_permission(user, requested, Permission.LEAVE_MANAGE)
        return requested
    return user.employee_id
