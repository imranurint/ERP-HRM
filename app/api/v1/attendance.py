"""Attendance and shift endpoints."""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.application.attendance.schemas import (
    AttendanceRead,
    AttendanceSummaryRow,
    CheckInRequest,
    CheckOutRequest,
    ManualAttendanceRequest,
    ShiftAssignmentCreate,
    ShiftAssignmentRead,
    ShiftCreate,
    ShiftRead,
    ShiftUpdate,
)
from app.application.attendance.service import AttendanceService
from app.core.deps import (
    ActorDep,
    CurrentUser,
    UowDep,
    ensure_self_or_permission,
    require,
)
from app.core.pagination import Page, PageParams, page_params
from app.domain.attendance.enums import AttendanceStatus
from app.domain.identity.exceptions import NotYourRecordError
from app.domain.identity.permissions import Permission

router = APIRouter(tags=["attendance"])

CompanyId = Annotated[UUID, Query(description="The company that owns this resource.")]
ReadDep = Depends(require(Permission.ATTENDANCE_READ))
WriteDep = Depends(require(Permission.ATTENDANCE_WRITE))
ManageDep = Depends(require(Permission.ORGANIZATION_MANAGE))


# --------------------------------------------------------------------- shifts
@router.post(
    "/shifts",
    response_model=ShiftRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a shift",
    dependencies=[ManageDep],
)
async def create_shift(
    company_id: CompanyId, payload: ShiftCreate, uow: UowDep, actor: ActorDep
) -> ShiftRead:
    """Overnight shifts are supported: an `end_time` at or before `start_time`
    means the shift finishes on the following calendar day."""
    return await AttendanceService(uow).create_shift(company_id, payload, actor)


@router.get(
    "/shifts", response_model=Page[ShiftRead], summary="List shifts", dependencies=[ReadDep]
)
async def list_shifts(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[ShiftRead]:
    return await AttendanceService(uow).list_shifts(company_id, params, is_active=is_active)


@router.patch(
    "/shifts/{shift_id}",
    response_model=ShiftRead,
    summary="Update a shift",
    dependencies=[ManageDep],
)
async def update_shift(
    company_id: CompanyId,
    shift_id: UUID,
    payload: ShiftUpdate,
    uow: UowDep,
    actor: ActorDep,
) -> ShiftRead:
    """Amending a shift does not rewrite past attendance — every record stores
    the figures derived at the time it was captured."""
    return await AttendanceService(uow).update_shift(company_id, shift_id, payload, actor)


@router.post(
    "/shifts/assignments",
    response_model=ShiftAssignmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Assign a shift to an employee",
    dependencies=[ManageDep],
)
async def assign_shift(
    company_id: CompanyId, payload: ShiftAssignmentCreate, uow: UowDep, actor: ActorDep
) -> ShiftAssignmentRead:
    return await AttendanceService(uow).assign_shift(company_id, payload, actor)


# ----------------------------------------------------------------- attendance
@router.post(
    "/attendance/check-in",
    response_model=AttendanceRead,
    summary="Record a check-in",
    dependencies=[WriteDep],
)
async def check_in(
    company_id: CompanyId,
    payload: CheckInRequest,
    uow: UowDep,
    user: CurrentUser,
    actor: ActorDep,
) -> AttendanceRead:
    """Omit `employee_id` to check yourself in.

    The record is filed under the calendar date in the *company's* timezone,
    not the server's. A repeat check-in on the same day is not an error — it
    returns the existing record.
    """
    employee_id = _resolve_employee(user, payload.employee_id, Permission.ATTENDANCE_WRITE)
    return await AttendanceService(uow).check_in(company_id, payload, employee_id, actor)


@router.post(
    "/attendance/check-out",
    response_model=AttendanceRead,
    summary="Record a check-out",
    dependencies=[WriteDep],
)
async def check_out(
    company_id: CompanyId,
    payload: CheckOutRequest,
    uow: UowDep,
    user: CurrentUser,
    actor: ActorDep,
) -> AttendanceRead:
    """An overnight worker checking out after midnight closes the *previous*
    day's record, which is the one their shift started on."""
    employee_id = _resolve_employee(user, payload.employee_id, Permission.ATTENDANCE_WRITE)
    return await AttendanceService(uow).check_out(company_id, payload, employee_id, actor)


@router.post(
    "/attendance/manual",
    response_model=AttendanceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record or amend a day by hand",
    dependencies=[WriteDep],
)
async def record_manual(
    company_id: CompanyId, payload: ManualAttendanceRequest, uow: UowDep, actor: ActorDep
) -> AttendanceRead:
    """Refused inside an APPROVED or PAID payroll period.

    The payslips derived from that attendance have already been signed off,
    and editing the inputs afterwards would invalidate them silently.
    """
    return await AttendanceService(uow).record_manual(company_id, payload, actor)


@router.get(
    "/attendance",
    response_model=Page[AttendanceRead],
    summary="List attendance records",
    dependencies=[ReadDep],
)
async def list_attendance(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    employee_id: Annotated[UUID | None, Query()] = None,
    department_id: Annotated[UUID | None, Query()] = None,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    attendance_status: Annotated[AttendanceStatus | None, Query()] = None,
) -> Page[AttendanceRead]:
    return await AttendanceService(uow).list_page(
        company_id,
        params,
        employee_id=employee_id,
        department_id=department_id,
        start_date=start_date,
        end_date=end_date,
        status=attendance_status,
    )


@router.get(
    "/attendance/summary",
    response_model=list[AttendanceSummaryRow],
    summary="Per-employee attendance totals",
    dependencies=[ReadDep],
)
async def attendance_summary(
    company_id: CompanyId,
    start_date: date,
    end_date: date,
    uow: UowDep,
    employee_id: Annotated[UUID | None, Query()] = None,
    department_id: Annotated[UUID | None, Query()] = None,
) -> list[AttendanceSummaryRow]:
    return await AttendanceService(uow).summarise(
        company_id,
        start_date,
        end_date,
        employee_id=employee_id,
        department_id=department_id,
    )


@router.get(
    "/attendance/{record_id}",
    response_model=AttendanceRead,
    summary="Fetch an attendance record",
    dependencies=[ReadDep],
)
async def get_attendance(company_id: CompanyId, record_id: UUID, uow: UowDep) -> AttendanceRead:
    return await AttendanceService(uow).get(company_id, record_id)


def _resolve_employee(user: CurrentUser, requested: UUID | None, permission: Permission) -> UUID:
    """Self-service resolution.

    With no `employee_id` the caller means themselves — and must therefore be
    linked to an employee record. Naming somebody else requires the
    permission, checked by `ensure_self_or_permission`.
    """
    if requested is None:
        if user.employee_id is None:
            raise NotYourRecordError(
                "Your login is not linked to an employee record; specify employee_id explicitly.",
                details={"user_id": str(user.id)},
            )
        return user.employee_id
    ensure_self_or_permission(user, requested, permission)
    return requested
