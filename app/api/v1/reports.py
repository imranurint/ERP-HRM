"""Report endpoints.

Every report answers in JSON by default and in CSV with `?format=csv`. The CSV
is streamed row by row, so a year of attendance for a large workforce never
has to be materialised in memory.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.application.reports import csv_export
from app.application.reports.schemas import (
    AttendanceSummaryReport,
    DepartmentHeadcountReport,
    EmployeeDirectoryReport,
    EmployeeLifecycleReport,
    LateAttendanceReport,
    LeaveBalanceReport,
    LeaveUsageReport,
    PayrollSummaryReport,
    PayslipReport,
    ReportFilters,
    SalaryComponentReport,
)
from app.application.reports.service import ReportService
from app.core.deps import UowDep, require
from app.core.pagination import PageParams, page_params
from app.domain.identity.permissions import Permission

router = APIRouter(prefix="/reports", tags=["reports"])

CompanyId = Annotated[UUID, Query(description="The company that owns this resource.")]
ReportsDep = Depends(require(Permission.REPORTS_READ))
PayrollReportsDep = Depends(require(Permission.PAYROLL_READ))

Format = Annotated[str, Query(pattern="^(json|csv)$", description="json (default) or csv")]


def _csv(rows: Iterator[str], filename: str) -> StreamingResponse:
    return StreamingResponse(
        rows,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
    )


def _filters(
    company_id: UUID,
    employee_id: UUID | None = None,
    department_id: UUID | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    year: int | None = None,
) -> ReportFilters:
    return ReportFilters(
        company_id=company_id,
        employee_id=employee_id,
        department_id=department_id,
        start_date=start_date,
        end_date=end_date,
        year=year,
    )


# ---------------------------------------------------------------- directory
@router.get(
    "/employee-directory",
    response_model=EmployeeDirectoryReport,
    summary="Employee directory",
    dependencies=[ReportsDep],
)
async def employee_directory(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    department_id: Annotated[UUID | None, Query()] = None,
    employee_id: Annotated[UUID | None, Query()] = None,
    response_format: Format = "json",
) -> EmployeeDirectoryReport | StreamingResponse:
    report = await ReportService(uow).employee_directory(
        _filters(company_id, employee_id=employee_id, department_id=department_id), params
    )
    if response_format == "csv":
        return _csv(csv_export.employee_directory_csv(report), "employee-directory")
    return report


@router.get(
    "/department-headcount",
    response_model=DepartmentHeadcountReport,
    summary="Headcount by department",
    dependencies=[ReportsDep],
)
async def department_headcount(
    company_id: CompanyId,
    uow: UowDep,
    as_of: Annotated[date | None, Query(description="Defaults to today.")] = None,
    response_format: Format = "json",
) -> DepartmentHeadcountReport | StreamingResponse:
    report = await ReportService(uow).department_headcount(_filters(company_id, end_date=as_of))
    if response_format == "csv":
        return _csv(csv_export.department_headcount_csv(report), "department-headcount")
    return report


# --------------------------------------------------------------- attendance
@router.get(
    "/attendance-summary",
    response_model=AttendanceSummaryReport,
    summary="Attendance summary",
    dependencies=[ReportsDep],
)
async def attendance_summary(
    company_id: CompanyId,
    start_date: date,
    end_date: date,
    uow: UowDep,
    employee_id: Annotated[UUID | None, Query()] = None,
    department_id: Annotated[UUID | None, Query()] = None,
    response_format: Format = "json",
) -> AttendanceSummaryReport | StreamingResponse:
    report = await ReportService(uow).attendance_summary(
        _filters(
            company_id,
            employee_id=employee_id,
            department_id=department_id,
            start_date=start_date,
            end_date=end_date,
        )
    )
    if response_format == "csv":
        return _csv(csv_export.attendance_summary_csv(report), "attendance-summary")
    return report


@router.get(
    "/late-attendance",
    response_model=LateAttendanceReport,
    summary="Late attendance",
    dependencies=[ReportsDep],
)
async def late_attendance(
    company_id: CompanyId,
    start_date: date,
    end_date: date,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    employee_id: Annotated[UUID | None, Query()] = None,
    minimum_minutes: Annotated[int, Query(ge=1)] = 1,
    response_format: Format = "json",
) -> LateAttendanceReport | StreamingResponse:
    report = await ReportService(uow).late_attendance(
        _filters(company_id, employee_id=employee_id, start_date=start_date, end_date=end_date),
        params,
        minimum_minutes=minimum_minutes,
    )
    if response_format == "csv":
        return _csv(csv_export.late_attendance_csv(report), "late-attendance")
    return report


# -------------------------------------------------------------------- leave
@router.get(
    "/leave-balances",
    response_model=LeaveBalanceReport,
    summary="Leave balances",
    dependencies=[ReportsDep],
)
async def leave_balances(
    company_id: CompanyId,
    uow: UowDep,
    employee_id: Annotated[UUID | None, Query()] = None,
    year: Annotated[int | None, Query(ge=1900, le=9999)] = None,
    response_format: Format = "json",
) -> LeaveBalanceReport | StreamingResponse:
    """Reads the `leave_balances` projection deliberately, so a discrepancy
    against `leave_transactions` is visible rather than hidden."""
    report = await ReportService(uow).leave_balances(
        _filters(company_id, employee_id=employee_id, year=year)
    )
    if response_format == "csv":
        return _csv(csv_export.leave_balance_csv(report), "leave-balances")
    return report


@router.get(
    "/leave-usage",
    response_model=LeaveUsageReport,
    summary="Leave usage",
    dependencies=[ReportsDep],
)
async def leave_usage(
    company_id: CompanyId,
    start_date: date,
    end_date: date,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    employee_id: Annotated[UUID | None, Query()] = None,
    response_format: Format = "json",
) -> LeaveUsageReport | StreamingResponse:
    report = await ReportService(uow).leave_usage(
        _filters(company_id, employee_id=employee_id, start_date=start_date, end_date=end_date),
        params,
    )
    if response_format == "csv":
        return _csv(csv_export.leave_usage_csv(report), "leave-usage")
    return report


# ------------------------------------------------------------------ payroll
@router.get(
    "/payroll-summary",
    response_model=PayrollSummaryReport,
    summary="Payroll summary for a run",
    dependencies=[PayrollReportsDep],
)
async def payroll_summary(
    company_id: CompanyId,
    run_id: UUID,
    uow: UowDep,
    response_format: Format = "json",
) -> PayrollSummaryReport | StreamingResponse:
    report = await ReportService(uow).payroll_summary(company_id, run_id)
    if response_format == "csv":
        return _csv(csv_export.payroll_summary_csv(report), "payroll-summary")
    return report


@router.get(
    "/payslip",
    response_model=PayslipReport,
    summary="A single payslip, formatted for presentation",
    dependencies=[PayrollReportsDep],
)
async def payslip(
    company_id: CompanyId,
    payslip_id: UUID,
    uow: UowDep,
    response_format: Format = "json",
) -> PayslipReport | StreamingResponse:
    report = await ReportService(uow).payslip(company_id, payslip_id)
    if response_format == "csv":
        return _csv(csv_export.payslip_csv(report), "payslip")
    return report


@router.get(
    "/salary-components",
    response_model=SalaryComponentReport,
    summary="Component totals for a run",
    dependencies=[PayrollReportsDep],
)
async def salary_components(
    company_id: CompanyId,
    run_id: UUID,
    uow: UowDep,
    response_format: Format = "json",
) -> SalaryComponentReport | StreamingResponse:
    """The same aggregation that becomes the accounting journal, grouped by
    component and GL account."""
    report = await ReportService(uow).salary_components(company_id, run_id)
    if response_format == "csv":
        return _csv(csv_export.salary_component_csv(report), "salary-components")
    return report


# ---------------------------------------------------------------- lifecycle
@router.get(
    "/employee-lifecycle",
    response_model=EmployeeLifecycleReport,
    summary="Employee lifecycle events",
    dependencies=[ReportsDep],
)
async def employee_lifecycle(
    company_id: CompanyId,
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    employee_id: Annotated[UUID | None, Query()] = None,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    response_format: Format = "json",
) -> EmployeeLifecycleReport | StreamingResponse:
    report = await ReportService(uow).employee_lifecycle(
        _filters(company_id, employee_id=employee_id, start_date=start_date, end_date=end_date),
        params,
    )
    if response_format == "csv":
        return _csv(csv_export.employee_lifecycle_csv(report), "employee-lifecycle")
    return report
