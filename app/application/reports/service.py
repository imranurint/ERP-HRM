"""Report service.

Reports read from the source tables, not from a cache or a summary table.
`leave_balances` is the one exception — it is the projection, and the leave
balance report reads it deliberately so that a discrepancy against
`leave_transactions` is visible rather than hidden. `make
rebuild-leave-balances` is the tool that proves they agree.

Every report is scoped by `company_id` before any other filter is applied.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from app.application.reports.schemas import (
    AttendanceSummaryReport,
    AttendanceSummaryReportRow,
    DepartmentHeadcountReport,
    DepartmentHeadcountRow,
    EmployeeDirectoryReport,
    EmployeeDirectoryRow,
    EmployeeLifecycleReport,
    LateAttendanceReport,
    LateAttendanceRow,
    LeaveBalanceReport,
    LeaveBalanceReportRow,
    LeaveUsageReport,
    LeaveUsageRow,
    LifecycleReportRow,
    PayrollSummaryReport,
    PayrollSummaryReportRow,
    PayslipLineRow,
    PayslipReport,
    ReportFilters,
    ReportMeta,
    SalaryComponentReport,
    SalaryComponentRow,
)
from app.core.dates import business_today, days_inclusive, utcnow
from app.core.money import ZERO
from app.core.pagination import PageParams
from app.domain.leave.rules import ZERO_DAYS, quantize_days
from app.domain.payroll.enums import SalaryComponentType
from app.domain.payroll.exceptions import PayrollRunNotFoundError, PayslipNotFoundError
from app.infrastructure.database.models.employee import Employee
from app.infrastructure.database.models.leave import LeaveType
from app.infrastructure.database.models.organization import Department
from app.infrastructure.database.unit_of_work import UnitOfWork

#: Reports may return more rows than a paged API listing. Still bounded — an
#: unbounded report is a way to take the database down by accident.
MAX_REPORT_ROWS = 5000


class ReportService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    # --------------------------------------------------------- directory
    async def employee_directory(
        self, filters: ReportFilters, params: PageParams
    ) -> EmployeeDirectoryReport:
        rows, _ = await self.uow.employees.list_paginated(
            filters.company_id,
            params,
            department_id=filters.department_id,
        )
        if filters.employee_id is not None:
            rows = [r for r in rows if r.id == filters.employee_id]

        departments = await self._department_names(filters.company_id)
        designations = await self._designation_names(filters.company_id)
        locations = await self._location_names(filters.company_id)
        managers = await self._employee_names(filters.company_id)
        today = business_today(await self._timezone(filters.company_id))

        directory = [
            EmployeeDirectoryRow(
                employee_id=employee.id,
                employee_number=employee.employee_number,
                full_name=employee.full_name,
                work_email=employee.work_email,
                department_name=departments.get(employee.department_id),
                designation_name=designations.get(employee.designation_id),
                location_name=locations.get(employee.location_id),
                manager_name=managers.get(employee.manager_id),
                employment_status=employee.employment_status,
                hire_date=employee.hire_date,
                termination_date=employee.termination_date,
                tenure_days=days_inclusive(employee.hire_date, employee.termination_date or today),
            )
            for employee in rows
        ]
        return EmployeeDirectoryReport(
            meta=self._meta("employee_directory", filters, len(directory)),
            rows=directory,
        )

    async def department_headcount(self, filters: ReportFilters) -> DepartmentHeadcountReport:
        as_of = filters.end_date or business_today(await self._timezone(filters.company_id))
        counts = await self.uow.employees.headcount_by_department(filters.company_id, as_of=as_of)
        departments = await self._departments(filters.company_id)

        rows = [
            DepartmentHeadcountRow(
                department_id=department_id,
                department_code=(
                    departments[department_id].department_code
                    if department_id in departments
                    else None
                ),
                department_name=(
                    departments[department_id].name
                    if department_id in departments
                    else "(unassigned)"
                ),
                headcount=count,
            )
            for department_id, count in counts
        ]
        rows.sort(key=lambda r: (-r.headcount, r.department_name))
        return DepartmentHeadcountReport(
            meta=self._meta("department_headcount", filters, len(rows)),
            rows=rows,
            total_headcount=sum(r.headcount for r in rows),
        )

    # -------------------------------------------------------- attendance
    async def attendance_summary(self, filters: ReportFilters) -> AttendanceSummaryReport:
        start, end = self._require_range(filters)
        from app.application.attendance.service import AttendanceService

        summaries = await AttendanceService(self.uow).summarise(
            filters.company_id,
            start,
            end,
            employee_id=filters.employee_id,
            department_id=filters.department_id,
        )
        rows = [
            AttendanceSummaryReportRow(
                employee_id=s.employee_id,
                employee_number=s.employee_number,
                full_name=s.full_name,
                present_days=s.present_days,
                absent_days=s.absent_days,
                late_days=s.late_days,
                half_days=s.half_days,
                leave_days=s.leave_days,
                holiday_days=s.holiday_days,
                weekend_days=s.weekend_days,
                total_working_minutes=s.total_working_minutes,
                total_overtime_minutes=s.total_overtime_minutes,
                total_late_minutes=s.total_late_minutes,
                payable_days=s.payable_days,
            )
            for s in summaries
        ]
        return AttendanceSummaryReport(
            meta=self._meta("attendance_summary", filters, len(rows)), rows=rows
        )

    async def late_attendance(
        self, filters: ReportFilters, params: PageParams, *, minimum_minutes: int = 1
    ) -> LateAttendanceReport:
        start, end = self._require_range(filters)
        records, _ = await self.uow.attendance.late_records(
            filters.company_id, start, end, params, minimum_minutes=minimum_minutes
        )
        if filters.employee_id is not None:
            records = [r for r in records if r.employee_id == filters.employee_id]

        names = await self._employee_lookup(filters.company_id, [r.employee_id for r in records])
        rows = [
            LateAttendanceRow(
                employee_id=record.employee_id,
                employee_number=(
                    names[record.employee_id].employee_number
                    if record.employee_id in names
                    else None
                ),
                full_name=(
                    names[record.employee_id].full_name if record.employee_id in names else None
                ),
                attendance_date=record.attendance_date,
                check_in=record.check_in,
                late_minutes=record.late_minutes,
            )
            for record in records
        ]
        return LateAttendanceReport(
            meta=self._meta("late_attendance", filters, len(rows)),
            rows=rows,
            total_late_minutes=sum(r.late_minutes for r in rows),
        )

    # ------------------------------------------------------------- leave
    async def leave_balances(self, filters: ReportFilters) -> LeaveBalanceReport:
        year = filters.year or business_today(await self._timezone(filters.company_id)).year
        employee_ids = [filters.employee_id] if filters.employee_id else None
        balances = await self.uow.leave_balances.list_for_company(
            filters.company_id, year, employee_ids=employee_ids
        )
        names = await self._employee_lookup(filters.company_id, [b.employee_id for b in balances])
        leave_types = await self._leave_type_lookup(filters.company_id)

        rows = [
            LeaveBalanceReportRow(
                employee_id=balance.employee_id,
                employee_number=(
                    names[balance.employee_id].employee_number
                    if balance.employee_id in names
                    else None
                ),
                full_name=(
                    names[balance.employee_id].full_name if balance.employee_id in names else None
                ),
                leave_code=(
                    leave_types[balance.leave_type_id].leave_code
                    if balance.leave_type_id in leave_types
                    else None
                ),
                leave_name=(
                    leave_types[balance.leave_type_id].name
                    if balance.leave_type_id in leave_types
                    else None
                ),
                year=balance.year,
                opening_balance=Decimal(str(balance.opening_balance)),
                earned=Decimal(str(balance.earned)),
                used=Decimal(str(balance.used)),
                adjustment=Decimal(str(balance.adjustment)),
                closing_balance=Decimal(str(balance.closing_balance)),
            )
            for balance in balances
        ]
        rows.sort(key=lambda r: (r.employee_number or "", r.leave_code or ""))
        return LeaveBalanceReport(meta=self._meta("leave_balance", filters, len(rows)), rows=rows)

    async def leave_usage(self, filters: ReportFilters, params: PageParams) -> LeaveUsageReport:
        start, end = self._require_range(filters)
        requests, _ = await self.uow.leave_requests.list_paginated(
            filters.company_id,
            params,
            employee_id=filters.employee_id,
            start_date=start,
            end_date=end,
        )
        names = await self._employee_lookup(filters.company_id, [r.employee_id for r in requests])
        rows = [
            LeaveUsageRow(
                employee_id=request.employee_id,
                employee_number=(
                    names[request.employee_id].employee_number
                    if request.employee_id in names
                    else None
                ),
                full_name=(
                    names[request.employee_id].full_name if request.employee_id in names else None
                ),
                leave_code=request.leave_type.leave_code,
                start_date=request.start_date,
                end_date=request.end_date,
                total_days=Decimal(str(request.total_days)),
                status=str(request.status),
                is_paid=request.leave_type.is_paid,
            )
            for request in requests
        ]
        return LeaveUsageReport(
            meta=self._meta("leave_usage", filters, len(rows)),
            rows=rows,
            total_days=quantize_days(sum((r.total_days for r in rows), start=ZERO_DAYS)),
        )

    # ----------------------------------------------------------- payroll
    async def payroll_summary(self, company_id: UUID, run_id: UUID) -> PayrollSummaryReport:
        run = await self.uow.payroll_runs.get_scoped(company_id, run_id)
        if run is None:
            raise PayrollRunNotFoundError(details={"run_id": str(run_id)})

        payslips = await self.uow.payslips.list_for_run(run.id)
        names = await self._employee_lookup(company_id, [p.employee_id for p in payslips])

        rows = [
            PayrollSummaryReportRow(
                employee_id=payslip.employee_id,
                employee_number=(
                    names[payslip.employee_id].employee_number
                    if payslip.employee_id in names
                    else None
                ),
                full_name=(
                    names[payslip.employee_id].full_name if payslip.employee_id in names else None
                ),
                gross_salary=Decimal(str(payslip.gross_salary)),
                total_deductions=Decimal(str(payslip.total_deductions)),
                employer_contributions=Decimal(str(payslip.employer_contributions)),
                net_salary=Decimal(str(payslip.net_salary)),
                currency_code=payslip.currency_code,
                status=payslip.status,
            )
            for payslip in payslips
        ]
        rows.sort(key=lambda r: r.employee_number or "")

        filters = ReportFilters(company_id=company_id)
        return PayrollSummaryReport(
            meta=self._meta("payroll_summary", filters, len(rows)),
            run_number=run.run_number,
            period_name=run.period.period_name,
            rows=rows,
            total_gross=Decimal(str(run.total_gross)),
            total_deductions=Decimal(str(run.total_deductions)),
            total_employer_contributions=Decimal(str(run.total_employer_contributions)),
            total_net=Decimal(str(run.total_net)),
        )

    async def payslip(self, company_id: UUID, payslip_id: UUID) -> PayslipReport:
        payslip = await self.uow.payslips.get_scoped(company_id, payslip_id)
        if payslip is None:
            raise PayslipNotFoundError(details={"payslip_id": str(payslip_id)})

        run = await self.uow.payroll_runs.get_scoped(company_id, payslip.payroll_run_id)
        assert run is not None
        employee = await self.uow.employees.get_scoped(company_id, payslip.employee_id)

        def lines(kind: SalaryComponentType) -> list[PayslipLineRow]:
            return [
                PayslipLineRow(
                    component_code=item.component_code,
                    component_name=item.component_name,
                    component_type=str(item.component_type),
                    amount=Decimal(str(item.amount)),
                    is_taxable=item.is_taxable,
                )
                for item in payslip.items
                if item.component_type is kind
            ]

        snapshot = payslip.calculation_snapshot or {}
        filters = ReportFilters(company_id=company_id, employee_id=payslip.employee_id)
        return PayslipReport(
            meta=self._meta("payslip", filters, len(payslip.items)),
            payslip_id=payslip.id,
            employee_number=employee.employee_number if employee else None,
            full_name=employee.full_name if employee else None,
            period_name=run.period.period_name,
            period_start=run.period.start_date,
            period_end=run.period.end_date,
            pay_date=run.period.pay_date,
            currency_code=payslip.currency_code,
            earnings=lines(SalaryComponentType.EARNING),
            deductions=lines(SalaryComponentType.DEDUCTION),
            employer_contributions=lines(SalaryComponentType.EMPLOYER_CONTRIBUTION),
            gross_salary=Decimal(str(payslip.gross_salary)),
            total_deductions=Decimal(str(payslip.total_deductions)),
            total_employer_contributions=Decimal(str(payslip.employer_contributions)),
            net_salary=Decimal(str(payslip.net_salary)),
            payable_days=Decimal(str(snapshot.get("payable_days", "0"))),
            working_days=Decimal(str(snapshot.get("working_days", "0"))),
        )

    async def salary_components(self, company_id: UUID, run_id: UUID) -> SalaryComponentReport:
        run = await self.uow.payroll_runs.get_scoped(company_id, run_id)
        if run is None:
            raise PayrollRunNotFoundError(details={"run_id": str(run_id)})

        totals = await self.uow.payslips.component_totals_for_run(run.id)
        payslips = await self.uow.payslips.list_for_run(run.id)
        counts: dict[str, int] = {}
        for payslip in payslips:
            for item in payslip.items:
                counts[item.component_code] = counts.get(item.component_code, 0) + 1

        rows: list[SalaryComponentRow] = []
        earnings = deductions = employer = ZERO
        for code, component_type, gl_account, amount in totals:
            kind = SalaryComponentType(component_type)
            if kind is SalaryComponentType.EARNING:
                earnings += amount
            elif kind is SalaryComponentType.DEDUCTION:
                deductions += amount
            else:
                employer += amount
            rows.append(
                SalaryComponentRow(
                    component_code=code,
                    component_type=str(kind),
                    gl_account_code=gl_account,
                    total_amount=amount,
                    employee_count=counts.get(code, 0),
                )
            )

        filters = ReportFilters(company_id=company_id)
        return SalaryComponentReport(
            meta=self._meta("salary_component", filters, len(rows)),
            run_number=run.run_number,
            rows=rows,
            total_earnings=earnings,
            total_deductions=deductions,
            total_employer_contributions=employer,
        )

    # --------------------------------------------------------- lifecycle
    async def employee_lifecycle(
        self, filters: ReportFilters, params: PageParams
    ) -> EmployeeLifecycleReport:
        if filters.employee_id is not None:
            events, _ = await self.uow.lifecycle.list_for_employee(filters.employee_id, params)
        else:
            events, _ = await self.uow.lifecycle.list_for_company(
                filters.company_id,
                params,
                start_date=filters.start_date,
                end_date=filters.end_date,
            )
        names = await self._employee_lookup(filters.company_id, [e.employee_id for e in events])
        rows = [
            LifecycleReportRow(
                employee_id=event.employee_id,
                employee_number=(
                    names[event.employee_id].employee_number if event.employee_id in names else None
                ),
                full_name=(
                    names[event.employee_id].full_name if event.employee_id in names else None
                ),
                event_type=event.event_type,
                effective_date=event.effective_date,
                reason=event.reason,
                created_at=event.created_at,
            )
            for event in events
        ]
        return EmployeeLifecycleReport(
            meta=self._meta("employee_lifecycle", filters, len(rows)), rows=rows
        )

    # --------------------------------------------------------- internals
    @staticmethod
    def _meta(name: str, filters: ReportFilters, row_count: int) -> ReportMeta:
        return ReportMeta(report=name, generated_at=utcnow(), filters=filters, row_count=row_count)

    @staticmethod
    def _require_range(filters: ReportFilters) -> tuple[date, date]:
        from app.core.exceptions import ValidationError

        if filters.start_date is None or filters.end_date is None:
            raise ValidationError(
                "This report requires both start_date and end_date.",
                details={"required": ["start_date", "end_date"]},
            )
        if filters.end_date < filters.start_date:
            raise ValidationError(
                "end_date cannot precede start_date.",
                details={
                    "start_date": filters.start_date.isoformat(),
                    "end_date": filters.end_date.isoformat(),
                },
            )
        return filters.start_date, filters.end_date

    async def _timezone(self, company_id: UUID) -> str | None:
        company = await self.uow.companies.get(company_id)
        return company.timezone if company else None

    async def _employee_lookup(
        self, company_id: UUID, employee_ids: Sequence[UUID]
    ) -> dict[UUID, Employee]:
        """One query for every name a report needs — never one per row."""
        unique = list(dict.fromkeys(employee_ids))
        if not unique:
            return {}
        rows = await self.uow.employees.list_payable(
            company_id, as_of=date.max, employee_ids=unique
        )
        found = {row.id: row for row in rows}
        # `list_payable` excludes INACTIVE employees, but a report over past
        # data must still be able to name them.
        for employee_id in unique:
            if employee_id not in found:
                employee = await self.uow.employees.get_scoped(company_id, employee_id)
                if employee is not None:
                    found[employee_id] = employee
        return found

    async def _employee_names(self, company_id: UUID) -> dict[UUID | None, str]:
        params = PageParams(page=1, page_size=200)
        rows, _ = await self.uow.employees.list_paginated(company_id, params)
        return {row.id: row.full_name for row in rows}

    async def _departments(self, company_id: UUID) -> dict[UUID, Department]:
        params = PageParams(page=1, page_size=200)
        rows, _ = await self.uow.departments.list_for_company(company_id, params)
        return {row.id: row for row in rows}

    async def _department_names(self, company_id: UUID) -> dict[UUID | None, str]:
        return {key: value.name for key, value in (await self._departments(company_id)).items()}

    async def _designation_names(self, company_id: UUID) -> dict[UUID | None, str]:
        params = PageParams(page=1, page_size=200)
        rows, _ = await self.uow.designations.list_for_company(company_id, params)
        return {row.id: row.name for row in rows}

    async def _location_names(self, company_id: UUID) -> dict[UUID | None, str]:
        params = PageParams(page=1, page_size=200)
        rows, _ = await self.uow.locations.list_for_company(company_id, params)
        return {row.id: row.name for row in rows}

    async def _leave_type_lookup(self, company_id: UUID) -> dict[UUID, LeaveType]:
        params = PageParams(page=1, page_size=200)
        rows, _ = await self.uow.leave_types.list_for_company(company_id, params)
        return {row.id: row for row in rows}


__all__ = ["MAX_REPORT_ROWS", "ReportService"]
