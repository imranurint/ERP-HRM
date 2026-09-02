"""CSV rendering for reports.

Streamed row by row so a year of attendance for a thousand people never has to
be materialised in memory. Amounts and day counts are written as plain decimal
strings with no thousands separators — the file is for spreadsheets and
downstream systems, not for display.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from decimal import Decimal

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
    SalaryComponentReport,
)


def _num(value: Decimal) -> str:
    return f"{value:f}"


class _Writer:
    """Adapter that lets csv.writer emit into a generator."""

    def __init__(self) -> None:
        self.buffer = io.StringIO()
        self.writer = csv.writer(self.buffer, lineterminator="\n")

    def row(self, values: list[str]) -> str:
        self.buffer.seek(0)
        self.buffer.truncate(0)
        self.writer.writerow(values)
        return self.buffer.getvalue()


def employee_directory_csv(report: EmployeeDirectoryReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(
        [
            "employee_number",
            "full_name",
            "work_email",
            "department",
            "designation",
            "location",
            "manager",
            "employment_status",
            "hire_date",
            "termination_date",
            "tenure_days",
        ]
    )
    for row in report.rows:
        yield out.row(
            [
                row.employee_number,
                row.full_name,
                row.work_email,
                row.department_name or "",
                row.designation_name or "",
                row.location_name or "",
                row.manager_name or "",
                str(row.employment_status),
                row.hire_date.isoformat(),
                row.termination_date.isoformat() if row.termination_date else "",
                str(row.tenure_days),
            ]
        )


def department_headcount_csv(report: DepartmentHeadcountReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(["department_code", "department_name", "headcount"])
    for row in report.rows:
        yield out.row([row.department_code or "", row.department_name, str(row.headcount)])
    yield out.row(["TOTAL", "", str(report.total_headcount)])


def attendance_summary_csv(report: AttendanceSummaryReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(
        [
            "employee_number",
            "full_name",
            "present_days",
            "absent_days",
            "late_days",
            "half_days",
            "leave_days",
            "holiday_days",
            "weekend_days",
            "total_working_minutes",
            "total_overtime_minutes",
            "total_late_minutes",
            "payable_days",
        ]
    )
    for row in report.rows:
        yield out.row(
            [
                row.employee_number or "",
                row.full_name or "",
                str(row.present_days),
                str(row.absent_days),
                str(row.late_days),
                str(row.half_days),
                str(row.leave_days),
                str(row.holiday_days),
                str(row.weekend_days),
                str(row.total_working_minutes),
                str(row.total_overtime_minutes),
                str(row.total_late_minutes),
                _num(row.payable_days),
            ]
        )


def late_attendance_csv(report: LateAttendanceReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(["employee_number", "full_name", "attendance_date", "check_in", "late_minutes"])
    for row in report.rows:
        yield out.row(
            [
                row.employee_number or "",
                row.full_name or "",
                row.attendance_date.isoformat(),
                row.check_in.isoformat() if row.check_in else "",
                str(row.late_minutes),
            ]
        )
    yield out.row(["TOTAL", "", "", "", str(report.total_late_minutes)])


def leave_balance_csv(report: LeaveBalanceReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(
        [
            "employee_number",
            "full_name",
            "leave_code",
            "leave_name",
            "year",
            "opening_balance",
            "earned",
            "used",
            "adjustment",
            "closing_balance",
        ]
    )
    for row in report.rows:
        yield out.row(
            [
                row.employee_number or "",
                row.full_name or "",
                row.leave_code or "",
                row.leave_name or "",
                str(row.year),
                _num(row.opening_balance),
                _num(row.earned),
                _num(row.used),
                _num(row.adjustment),
                _num(row.closing_balance),
            ]
        )


def leave_usage_csv(report: LeaveUsageReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(
        [
            "employee_number",
            "full_name",
            "leave_code",
            "start_date",
            "end_date",
            "total_days",
            "status",
            "is_paid",
        ]
    )
    for row in report.rows:
        yield out.row(
            [
                row.employee_number or "",
                row.full_name or "",
                row.leave_code or "",
                row.start_date.isoformat(),
                row.end_date.isoformat(),
                _num(row.total_days),
                row.status,
                "yes" if row.is_paid else "no",
            ]
        )
    yield out.row(["TOTAL", "", "", "", "", _num(report.total_days), "", ""])


def payroll_summary_csv(report: PayrollSummaryReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(
        [
            "employee_number",
            "full_name",
            "gross_salary",
            "total_deductions",
            "employer_contributions",
            "net_salary",
            "currency_code",
            "status",
        ]
    )
    for row in report.rows:
        yield out.row(
            [
                row.employee_number or "",
                row.full_name or "",
                _num(row.gross_salary),
                _num(row.total_deductions),
                _num(row.employer_contributions),
                _num(row.net_salary),
                row.currency_code,
                str(row.status),
            ]
        )
    yield out.row(
        [
            "TOTAL",
            "",
            _num(report.total_gross),
            _num(report.total_deductions),
            _num(report.total_employer_contributions),
            _num(report.total_net),
            "",
            "",
        ]
    )


def payslip_csv(report: PayslipReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(["section", "component_code", "component_name", "amount", "is_taxable"])
    for section, rows in (
        ("EARNING", report.earnings),
        ("DEDUCTION", report.deductions),
        ("EMPLOYER_CONTRIBUTION", report.employer_contributions),
    ):
        for row in rows:
            yield out.row(
                [
                    section,
                    row.component_code,
                    row.component_name,
                    _num(row.amount),
                    "yes" if row.is_taxable else "no",
                ]
            )
    yield out.row(["TOTAL", "GROSS", "Gross salary", _num(report.gross_salary), ""])
    yield out.row(["TOTAL", "DEDUCTIONS", "Total deductions", _num(report.total_deductions), ""])
    yield out.row(["TOTAL", "NET", "Net salary", _num(report.net_salary), ""])


def salary_component_csv(report: SalaryComponentReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(
        ["component_code", "component_type", "gl_account_code", "total_amount", "employee_count"]
    )
    for row in report.rows:
        yield out.row(
            [
                row.component_code,
                row.component_type,
                row.gl_account_code or "",
                _num(row.total_amount),
                str(row.employee_count),
            ]
        )
    yield out.row(["TOTAL", "EARNING", "", _num(report.total_earnings), ""])
    yield out.row(["TOTAL", "DEDUCTION", "", _num(report.total_deductions), ""])
    yield out.row(
        ["TOTAL", "EMPLOYER_CONTRIBUTION", "", _num(report.total_employer_contributions), ""]
    )


def employee_lifecycle_csv(report: EmployeeLifecycleReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(
        ["employee_number", "full_name", "event_type", "effective_date", "reason", "created_at"]
    )
    for row in report.rows:
        yield out.row(
            [
                row.employee_number or "",
                row.full_name or "",
                str(row.event_type),
                row.effective_date.isoformat(),
                row.reason or "",
                row.created_at.isoformat(),
            ]
        )
