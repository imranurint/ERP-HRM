"""Report schemas.

Every report shares the same envelope — the report's name, the filters it was
run with, when it was generated, and its rows. That makes a CSV export and a
JSON response two renderings of one object rather than two code paths that
drift apart.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from app.application.common import AppModel, DaysStr, MoneyStr
from app.domain.employees.enums import EmploymentStatus, LifecycleEventType
from app.domain.payroll.enums import PayslipStatus


class ReportFilters(AppModel):
    company_id: UUID
    employee_id: UUID | None = None
    department_id: UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    year: int | None = None


class ReportMeta(AppModel):
    report: str
    generated_at: datetime
    filters: ReportFilters
    row_count: int


# --------------------------------------------------------------- directory
class EmployeeDirectoryRow(AppModel):
    employee_id: UUID
    employee_number: str
    full_name: str
    work_email: str
    department_name: str | None = None
    designation_name: str | None = None
    location_name: str | None = None
    manager_name: str | None = None
    employment_status: EmploymentStatus
    hire_date: date
    termination_date: date | None = None
    tenure_days: int


class EmployeeDirectoryReport(AppModel):
    meta: ReportMeta
    rows: list[EmployeeDirectoryRow]


# --------------------------------------------------------------- headcount
class DepartmentHeadcountRow(AppModel):
    department_id: UUID | None
    department_code: str | None
    department_name: str
    headcount: int


class DepartmentHeadcountReport(AppModel):
    meta: ReportMeta
    rows: list[DepartmentHeadcountRow]
    total_headcount: int


# -------------------------------------------------------------- attendance
class AttendanceSummaryReportRow(AppModel):
    employee_id: UUID
    employee_number: str | None
    full_name: str | None
    present_days: int
    absent_days: int
    late_days: int
    half_days: int
    leave_days: int
    holiday_days: int
    weekend_days: int
    total_working_minutes: int
    total_overtime_minutes: int
    total_late_minutes: int
    payable_days: DaysStr


class AttendanceSummaryReport(AppModel):
    meta: ReportMeta
    rows: list[AttendanceSummaryReportRow]


class LateAttendanceRow(AppModel):
    employee_id: UUID
    employee_number: str | None
    full_name: str | None
    attendance_date: date
    check_in: datetime | None
    late_minutes: int


class LateAttendanceReport(AppModel):
    meta: ReportMeta
    rows: list[LateAttendanceRow]
    total_late_minutes: int


# ------------------------------------------------------------------- leave
class LeaveBalanceReportRow(AppModel):
    employee_id: UUID
    employee_number: str | None
    full_name: str | None
    leave_code: str | None
    leave_name: str | None
    year: int
    opening_balance: DaysStr
    earned: DaysStr
    used: DaysStr
    adjustment: DaysStr
    closing_balance: DaysStr


class LeaveBalanceReport(AppModel):
    meta: ReportMeta
    rows: list[LeaveBalanceReportRow]


class LeaveUsageRow(AppModel):
    employee_id: UUID
    employee_number: str | None
    full_name: str | None
    leave_code: str | None
    start_date: date
    end_date: date
    total_days: DaysStr
    status: str
    is_paid: bool


class LeaveUsageReport(AppModel):
    meta: ReportMeta
    rows: list[LeaveUsageRow]
    total_days: DaysStr


# ----------------------------------------------------------------- payroll
class PayrollSummaryReportRow(AppModel):
    employee_id: UUID
    employee_number: str | None
    full_name: str | None
    gross_salary: MoneyStr
    total_deductions: MoneyStr
    employer_contributions: MoneyStr
    net_salary: MoneyStr
    currency_code: str
    status: PayslipStatus


class PayrollSummaryReport(AppModel):
    meta: ReportMeta
    run_number: str
    period_name: str
    rows: list[PayrollSummaryReportRow]
    total_gross: MoneyStr
    total_deductions: MoneyStr
    total_employer_contributions: MoneyStr
    total_net: MoneyStr


class PayslipLineRow(AppModel):
    component_code: str
    component_name: str
    component_type: str
    amount: MoneyStr
    is_taxable: bool


class PayslipReport(AppModel):
    """A single payslip, formatted for presentation to the employee."""

    meta: ReportMeta
    payslip_id: UUID
    employee_number: str | None
    full_name: str | None
    period_name: str
    period_start: date | None
    period_end: date | None
    pay_date: date | None
    currency_code: str
    earnings: list[PayslipLineRow]
    deductions: list[PayslipLineRow]
    employer_contributions: list[PayslipLineRow]
    gross_salary: MoneyStr
    total_deductions: MoneyStr
    total_employer_contributions: MoneyStr
    net_salary: MoneyStr
    payable_days: DaysStr
    working_days: DaysStr


class SalaryComponentRow(AppModel):
    component_code: str
    component_type: str
    gl_account_code: str | None
    total_amount: MoneyStr
    employee_count: int


class SalaryComponentReport(AppModel):
    meta: ReportMeta
    run_number: str
    rows: list[SalaryComponentRow]
    total_earnings: MoneyStr
    total_deductions: MoneyStr
    total_employer_contributions: MoneyStr


# --------------------------------------------------------------- lifecycle
class LifecycleReportRow(AppModel):
    employee_id: UUID
    employee_number: str | None
    full_name: str | None
    event_type: LifecycleEventType
    effective_date: date
    reason: str | None
    created_at: datetime


class EmployeeLifecycleReport(AppModel):
    meta: ReportMeta
    rows: list[LifecycleReportRow]


ReportFormat = str

__all__ = [
    "AttendanceSummaryReport",
    "AttendanceSummaryReportRow",
    "DepartmentHeadcountReport",
    "DepartmentHeadcountRow",
    "EmployeeDirectoryReport",
    "EmployeeDirectoryRow",
    "EmployeeLifecycleReport",
    "LateAttendanceReport",
    "LateAttendanceRow",
    "LeaveBalanceReport",
    "LeaveBalanceReportRow",
    "LeaveUsageReport",
    "LeaveUsageRow",
    "LifecycleReportRow",
    "PayrollSummaryReport",
    "PayrollSummaryReportRow",
    "PayslipLineRow",
    "PayslipReport",
    "ReportFilters",
    "ReportMeta",
    "SalaryComponentReport",
    "SalaryComponentRow",
]
