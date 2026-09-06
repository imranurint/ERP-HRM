"""Payroll service — the module's most consequential code.

Every state transition follows the same shape:

    lock the run → lock the period → check the Golden Rule guard →
    mutate → audit → outbox → single COMMIT

The two row locks are what make it safe under concurrency. Locking the run
stops two operators approving the same payroll at once. Locking the period
stops somebody closing it halfway through a run against it, which would leave
payslips referencing a period that never accepted them.

Nothing here edits approved or paid payroll. Golden Rule 7 says a correction
is a *new* adjustment run (`adjusts_run_id`), so the original and the
correction both stay visible forever.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.config import settings
from app.core.dates import utcnow
from app.core.exceptions import NotFoundError
from app.core.money import ZERO, quantize_currency
from app.core.pagination import Page, PageParams
from app.modules.payroll.application.attendance_resolver import PeriodWindow, resolve
from app.modules.payroll.application.schemas import (
    PayrollApprovalPreview,
    PayrollCalculateRequest,
    PayrollJournalLine,
    PayrollPeriodCreate,
    PayrollPeriodRead,
    PayrollRunCreate,
    PayrollRunRead,
    PayrollSummaryRow,
    PayslipRead,
    VoidPayslipRequest,
)
from app.modules.payroll.domain.calculator import (
    BASIC_SALARY_CODE,
    PayrollInput,
    ResolvedComponent,
    calculate,
)
from app.modules.payroll.domain.enums import (
    PayrollPeriodStatus,
    PayrollRunStatus,
    PayslipStatus,
    SalaryComponentType,
)
from app.modules.payroll.domain.exceptions import (
    PayrollEmptyError,
    PayrollPeriodNotFoundError,
    PayrollRunInProgressError,
    PayrollRunNotFoundError,
    PayslipNotFoundError,
)
from app.modules.payroll.domain.rules import (
    ensure_can_approve,
    ensure_can_pay,
    ensure_no_period_overlap,
    ensure_pay_date,
    ensure_payslip_voidable,
    ensure_period_accepts_adjustment,
    ensure_period_dates,
    ensure_period_open,
    ensure_run_mutable,
)
from app.modules.payroll.infrastructure.models import (
    PayrollPeriod,
    PayrollRun,
    Payslip,
    PayslipItem,
)
from app.shared.context import ActorContext
from app.shared.enums import AuditAction
from app.shared.events.domain_events import PayrollApproved, PayrollCalculated, PayrollPaid
from app.shared.observability import metrics
from app.shared.unit_of_work.unit_of_work import UnitOfWork

NET_PAY_CODE = "NET_PAY"
EMPLOYER_LIABILITY_CODE = "EMPLOYER_LIABILITY"


class PayrollService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    # --------------------------------------------------------------- periods
    async def create_period(
        self, company_id: UUID, payload: PayrollPeriodCreate, actor: ActorContext
    ) -> PayrollPeriodRead:
        """Golden Rule 1: payroll periods cannot overlap."""
        if await self.uow.companies.get(company_id) is None:
            raise NotFoundError("Company not found.", details={"company_id": str(company_id)})

        ensure_period_dates(payload.start_date, payload.end_date)
        ensure_pay_date(payload.start_date, payload.pay_date)

        # Checked here for a good message, and by a GiST exclusion constraint
        # for actual safety — two simultaneous inserts both pass this check.
        existing = await self.uow.payroll_periods.existing_ranges(company_id)
        ensure_no_period_overlap(payload.start_date, payload.end_date, existing)

        period = PayrollPeriod(
            company_id=company_id,
            period_name=payload.period_name,
            start_date=payload.start_date,
            end_date=payload.end_date,
            pay_date=payload.pay_date,
            status=PayrollPeriodStatus.OPEN,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.payroll_periods.add(period)
        await self.uow.flush()

        self._audit(
            AuditAction.PAYROLL_PERIOD_CREATED,
            "PayrollPeriod",
            period.id,
            company_id,
            actor,
            new_data={
                "period_name": period.period_name,
                "start_date": period.start_date.isoformat(),
                "end_date": period.end_date.isoformat(),
            },
        )
        await self.uow.commit()
        await self.uow.refresh(period)
        return PayrollPeriodRead.model_validate(period)

    async def close_period(
        self, company_id: UUID, period_id: UUID, actor: ActorContext
    ) -> PayrollPeriodRead:
        period = await self._require_period_locked(company_id, period_id)
        previous = {"status": str(period.status)}
        period.status = PayrollPeriodStatus.CLOSED
        period.closed_at = utcnow()
        period.closed_by = actor.user_id
        period.updated_by = actor.user_id

        self._audit(
            AuditAction.PAYROLL_PERIOD_CLOSED,
            "PayrollPeriod",
            period.id,
            company_id,
            actor,
            previous_data=previous,
            new_data={"status": str(period.status)},
        )
        await self.uow.commit()
        return PayrollPeriodRead.model_validate(period)

    async def list_periods(
        self, company_id: UUID, params: PageParams, *, status: PayrollPeriodStatus | None = None
    ) -> Page[PayrollPeriodRead]:
        rows, total = await self.uow.payroll_periods.list_paginated(
            company_id, params, status=status
        )
        return Page.build([PayrollPeriodRead.model_validate(r) for r in rows], params, total=total)

    async def get_period(self, company_id: UUID, period_id: UUID) -> PayrollPeriodRead:
        return PayrollPeriodRead.model_validate(await self._require_period(company_id, period_id))

    # ------------------------------------------------------------------ runs
    async def create_run(
        self, company_id: UUID, payload: PayrollRunCreate, actor: ActorContext
    ) -> PayrollRunRead:
        period = await self._require_period_locked(company_id, payload.payroll_period_id)

        if payload.adjusts_run_id is not None:
            # A correction must be able to enter a period its own approval
            # closed — otherwise Golden Rule 7 could never be obeyed.
            await self._require_run(company_id, payload.adjusts_run_id)
            ensure_period_accepts_adjustment(period.status)
        else:
            ensure_period_open(period.status)

        # Two concurrent ordinary runs would each produce a full set of
        # payslips for the same period and double the payroll.
        if await self.uow.payroll_runs.live_run_exists(period.id):
            raise PayrollRunInProgressError(details={"payroll_period_id": str(period.id)})

        company = await self.uow.companies.get(company_id)
        assert company is not None

        run_number = await self.uow.sequences.allocate_payroll_run_number(
            company_id, period.start_date.year
        )
        run = PayrollRun(
            company_id=company_id,
            payroll_period_id=period.id,
            run_number=run_number,
            status=PayrollRunStatus.DRAFT,
            adjusts_run_id=payload.adjusts_run_id,
            notes=payload.notes,
            currency_code=company.base_currency_code,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.payroll_runs.add(run)
        await self.uow.flush()

        self._audit(
            AuditAction.PAYROLL_RUN_CREATED,
            "PayrollRun",
            run.id,
            company_id,
            actor,
            new_data={
                "run_number": run.run_number,
                "payroll_period_id": str(period.id),
                "adjusts_run_id": str(payload.adjusts_run_id) if payload.adjusts_run_id else None,
            },
        )
        await self.uow.commit()
        await self.uow.refresh(run)
        return PayrollRunRead.model_validate(run)

    async def calculate(
        self,
        company_id: UUID,
        run_id: UUID,
        payload: PayrollCalculateRequest,
        actor: ActorContext,
    ) -> PayrollRunRead:
        """Compute every payslip in the run, inside one transaction.

        Recalculating discards the previous payslips and computes from
        scratch. That is safe *because* `ensure_run_mutable` has already
        refused if the run is approved or paid, and because the calculation is
        deterministic — the same inputs always produce the same numbers.
        """
        started = time.perf_counter()
        run = await self._require_run_locked(company_id, run_id)
        ensure_run_mutable(run.status)

        period = await self._require_period_locked(company_id, run.payroll_period_id)
        currency = await self.uow.currencies.get_by_code(run.currency_code)
        if currency is None:
            raise NotFoundError(
                "Run currency not found.", details={"currency_code": run.currency_code}
            )

        run.status = PayrollRunStatus.PROCESSING
        run.started_at = utcnow()
        run.failure_reason = None
        await self.uow.flush()

        try:
            await self.uow.payslips.delete_for_run(run.id)
            await self.uow.flush()

            employees = await self.uow.employees.list_payable(
                company_id,
                as_of=period.end_date,
                employee_ids=payload.employee_ids or None,
            )
            if not employees:
                raise PayrollEmptyError(
                    "No employees are payable for this period.",
                    details={"payroll_period_id": str(period.id)},
                )

            employee_ids = [e.id for e in employees]
            window = PeriodWindow(period.start_date, period.end_date)

            # Everything the run needs, in a fixed number of queries. Fetching
            # per employee would make a thousand-person payroll a thousand
            # round-trips.
            structures = await self.uow.salary_structures.effective_on_bulk(
                employee_ids, period.end_date
            )
            attendance = await self.uow.attendance.list_for_range(
                company_id, employee_ids, period.start_date, period.end_date
            )
            approved_leave = await self.uow.leave_requests.approved_in_range(
                company_id, employee_ids, period.start_date, period.end_date
            )
            holidays = frozenset(
                await self.uow.holidays.dates_between(
                    company_id, period.start_date, period.end_date
                )
            )

            skipped: list[dict[str, str]] = []
            payslips: list[Payslip] = []

            for employee in employees:
                structure = structures.get(employee.id)
                if structure is None:
                    # Not a failure: a director on no salary structure simply
                    # is not on this payroll. Recorded so it is visible.
                    skipped.append(
                        {
                            "employee_id": str(employee.id),
                            "employee_number": employee.employee_number,
                            "reason": "NO_ACTIVE_SALARY_STRUCTURE",
                        }
                    )
                    continue

                summary = resolve(
                    employee,
                    window,
                    holidays=holidays,
                    attendance=attendance,
                    approved_leave=approved_leave,
                )
                components = tuple(
                    ResolvedComponent(
                        component_id=line.salary_component_id,
                        component_code=line.component.component_code,
                        name=line.component.name,
                        component_type=line.component.component_type,
                        calculation_type=line.component.calculation_type,
                        sequence=(
                            line.sequence_override
                            if line.sequence_override is not None
                            else line.component.sequence
                        ),
                        is_taxable=line.component.is_taxable,
                        amount=Decimal(str(line.amount)),
                        percentage=(
                            Decimal(str(line.percentage)) if line.percentage is not None else None
                        ),
                        percentage_base=line.component.percentage_base,
                        formula_key=line.component.formula_key,
                        gl_account_code=line.component.gl_account_code,
                    )
                    for line in structure.components
                    if line.component.is_active
                )

                result = calculate(
                    PayrollInput(
                        employee_id=employee.id,
                        base_salary=Decimal(str(structure.base_salary)),
                        currency_code=structure.currency_code,
                        currency_decimal_places=currency.decimal_places,
                        components=components,
                        attendance=summary,
                        prorate_base_salary=structure.prorate_base_salary,
                    )
                )

                payslip = Payslip(
                    company_id=company_id,
                    payroll_run_id=run.id,
                    employee_id=employee.id,
                    salary_structure_id=structure.id,
                    gross_salary=result.gross_salary,
                    total_deductions=result.total_deductions,
                    employer_contributions=result.employer_contributions,
                    net_salary=result.net_salary,
                    currency_code=structure.currency_code,
                    status=PayslipStatus.CALCULATED,
                    # Golden Rule 6: the complete set of inputs, stored with
                    # the payslip so it can be explained years later without
                    # depending on the employee's current configuration.
                    calculation_snapshot={
                        **result.snapshot,
                        "employee_number": employee.employee_number,
                        "salary_structure_id": str(structure.id),
                        "period": {
                            "id": str(period.id),
                            "name": period.period_name,
                            "start_date": period.start_date.isoformat(),
                            "end_date": period.end_date.isoformat(),
                        },
                        "run_number": run.run_number,
                    },
                    created_by=actor.user_id,
                    updated_by=actor.user_id,
                )
                self.uow.payslips.add(payslip)
                await self.uow.flush()

                for line in result.lines:
                    self.uow.session.add(
                        PayslipItem(
                            payslip_id=payslip.id,
                            salary_component_id=line.component_id,
                            component_code=line.component_code,
                            # Copied, not joined: renaming a component next
                            # year must not rewrite an issued payslip.
                            component_name=line.component_name,
                            component_type=line.component_type,
                            amount=line.amount,
                            is_taxable=line.is_taxable,
                            sequence=line.sequence,
                            gl_account_code=line.gl_account_code,
                            calculation_metadata=line.calculation_metadata,
                        )
                    )
                payslips.append(payslip)

            await self.uow.flush()

            if not payslips:
                raise PayrollEmptyError(
                    "No employee in this period has an active salary structure.",
                    details={"skipped": skipped},
                )

            count, gross, deductions, employer, net = await self.uow.payslips.run_totals(run.id)
            run.payslip_count = count
            run.total_gross = gross
            run.total_deductions = deductions
            run.total_employer_contributions = employer
            run.total_net = net
            run.status = PayrollRunStatus.COMPLETED
            run.completed_at = utcnow()
            run.updated_by = actor.user_id

            self._audit(
                AuditAction.PAYROLL_CALCULATED,
                "PayrollRun",
                run.id,
                company_id,
                actor,
                new_data={
                    "run_number": run.run_number,
                    "payslip_count": count,
                    "total_gross": str(gross),
                    "total_net": str(net),
                    "skipped": skipped,
                },
            )
            self.uow.outbox.enqueue(
                PayrollCalculated(
                    aggregate_id=run.id,
                    company_id=company_id,
                    run_number=run.run_number,
                    payroll_period_id=period.id,
                    payslip_count=count,
                    total_gross=gross,
                    total_net=net,
                    currency_code=run.currency_code,
                    calculated_by=actor.user_id,
                )
            )
            await self.uow.commit()

        except Exception as exc:
            # The whole calculation rolled back. Record the failure in its own
            # transaction so the run does not sit in PROCESSING forever with
            # nothing explaining why.
            await self.uow.rollback()
            await self._mark_failed(company_id, run_id, str(exc), actor)
            metrics.payroll_failures_total.labels(getattr(exc, "code", type(exc).__name__)).inc()
            raise

        metrics.payroll_calculation_duration_seconds.observe(time.perf_counter() - started)
        metrics.payslips_generated_total.labels(str(company_id)).inc(run.payslip_count)
        await self.uow.refresh(run)
        return PayrollRunRead.model_validate(run)

    async def approve(self, company_id: UUID, run_id: UUID, actor: ActorContext) -> PayrollRunRead:
        """Approve a calculated run. After this the numbers are immutable."""
        run = await self._require_run_locked(company_id, run_id)
        ensure_can_approve(run.status)

        period = await self._require_period_locked(company_id, run.payroll_period_id)
        previous = {"status": str(run.status)}

        run.status = PayrollRunStatus.APPROVED
        run.approved_at = utcnow()
        run.approved_by = actor.user_id
        run.updated_by = actor.user_id
        await self.uow.payslips.set_status_for_run(run.id, PayslipStatus.APPROVED)

        # Locking the attendance and leave underneath: the payslips derived
        # from them have now been signed off.
        period.status = PayrollPeriodStatus.APPROVED

        lines = await self._journal_lines(run)
        self._audit(
            AuditAction.PAYROLL_APPROVED,
            "PayrollRun",
            run.id,
            company_id,
            actor,
            previous_data=previous,
            new_data={
                "status": str(run.status),
                "payslip_count": run.payslip_count,
                "total_net": str(run.total_net),
            },
        )
        self.uow.outbox.enqueue(
            PayrollApproved(
                aggregate_id=run.id,
                company_id=company_id,
                run_number=run.run_number,
                payroll_period_id=period.id,
                period_name=period.period_name,
                period_start=period.start_date,
                period_end=period.end_date,
                pay_date=period.pay_date,
                currency_code=run.currency_code,
                payslip_count=run.payslip_count,
                total_gross=Decimal(str(run.total_gross)),
                total_deductions=Decimal(str(run.total_deductions)),
                total_employer_contributions=Decimal(str(run.total_employer_contributions)),
                total_net=Decimal(str(run.total_net)),
                approved_by=actor.user_id,
                journal_lines=[line.model_dump(mode="json") for line in lines],
            )
        )
        await self.uow.commit()

        metrics.payroll_runs_total.labels(str(company_id), "approved").inc()
        return PayrollRunRead.model_validate(run)

    async def pay(self, company_id: UUID, run_id: UUID, actor: ActorContext) -> PayrollRunRead:
        run = await self._require_run_locked(company_id, run_id)
        ensure_can_pay(run.status)

        period = await self._require_period_locked(company_id, run.payroll_period_id)
        previous = {"status": str(run.status)}

        run.status = PayrollRunStatus.PAID
        run.paid_at = utcnow()
        run.paid_by = actor.user_id
        run.updated_by = actor.user_id
        await self.uow.payslips.set_status_for_run(run.id, PayslipStatus.PAID)
        period.status = PayrollPeriodStatus.PAID

        self._audit(
            AuditAction.PAYROLL_PAID,
            "PayrollRun",
            run.id,
            company_id,
            actor,
            previous_data=previous,
            new_data={"status": str(run.status), "total_net": str(run.total_net)},
        )
        self.uow.outbox.enqueue(
            PayrollPaid(
                aggregate_id=run.id,
                company_id=company_id,
                run_number=run.run_number,
                payroll_period_id=period.id,
                pay_date=period.pay_date,
                total_net=Decimal(str(run.total_net)),
                currency_code=run.currency_code,
                paid_by=actor.user_id,
            )
        )
        await self.uow.commit()

        metrics.payroll_runs_total.labels(str(company_id), "paid").inc()
        return PayrollRunRead.model_validate(run)

    # -------------------------------------------------------------- payslips
    async def get_payslip(self, company_id: UUID, payslip_id: UUID) -> PayslipRead:
        payslip = await self.uow.payslips.get_scoped(company_id, payslip_id)
        if payslip is None:
            raise PayslipNotFoundError(details={"payslip_id": str(payslip_id)})
        return PayslipRead.model_validate(payslip)

    async def void_payslip(
        self, company_id: UUID, payslip_id: UUID, payload: VoidPayslipRequest, actor: ActorContext
    ) -> PayslipRead:
        """Exclude a payslip from a run without deleting it.

        Refused once paid: money has left the building and the correction is
        an adjustment run, which leaves both entries visible.
        """
        payslip = await self.uow.payslips.get_scoped(company_id, payslip_id)
        if payslip is None:
            raise PayslipNotFoundError(details={"payslip_id": str(payslip_id)})
        ensure_payslip_voidable(payslip.status)

        run = await self._require_run_locked(company_id, payslip.payroll_run_id)
        previous = {"status": str(payslip.status)}

        payslip.status = PayslipStatus.VOID
        payslip.void_reason = payload.reason
        payslip.updated_by = actor.user_id
        await self.uow.flush()

        count, gross, deductions, employer, net = await self.uow.payslips.run_totals(run.id)
        run.payslip_count = count
        run.total_gross = gross
        run.total_deductions = deductions
        run.total_employer_contributions = employer
        run.total_net = net

        self._audit(
            AuditAction.PAYSLIP_VOIDED,
            "Payslip",
            payslip.id,
            company_id,
            actor,
            previous_data=previous,
            new_data={"status": str(payslip.status), "reason": payload.reason},
        )
        await self.uow.commit()
        return PayslipRead.model_validate(payslip)

    async def list_payslips(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        employee_id: UUID | None = None,
        run_id: UUID | None = None,
        status: PayslipStatus | None = None,
    ) -> Page[PayslipRead]:
        rows, total = await self.uow.payslips.list_paginated(
            company_id, params, employee_id=employee_id, run_id=run_id, status=status
        )
        return Page.build([PayslipRead.model_validate(r) for r in rows], params, total=total)

    # ----------------------------------------------------------------- reads
    async def get_run(self, company_id: UUID, run_id: UUID) -> PayrollRunRead:
        return PayrollRunRead.model_validate(await self._require_run(company_id, run_id))

    async def list_runs(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        period_id: UUID | None = None,
        status: PayrollRunStatus | None = None,
    ) -> Page[PayrollRunRead]:
        rows, total = await self.uow.payroll_runs.list_paginated(
            company_id, params, period_id=period_id, status=status
        )
        return Page.build([PayrollRunRead.model_validate(r) for r in rows], params, total=total)

    async def run_summary(self, company_id: UUID, run_id: UUID) -> list[PayrollSummaryRow]:
        run = await self._require_run(company_id, run_id)
        payslips = await self.uow.payslips.list_for_run(run.id)
        employees = await self.uow.employees.list_payable(
            company_id, as_of=date.max, employee_ids=[p.employee_id for p in payslips]
        )
        by_id = {e.id: e for e in employees}
        return [
            PayrollSummaryRow(
                employee_id=payslip.employee_id,
                employee_number=(
                    by_id[payslip.employee_id].employee_number
                    if payslip.employee_id in by_id
                    else None
                ),
                full_name=(
                    by_id[payslip.employee_id].full_name if payslip.employee_id in by_id else None
                ),
                gross_salary=Decimal(str(payslip.gross_salary)),
                total_deductions=Decimal(str(payslip.total_deductions)),
                employer_contributions=Decimal(str(payslip.employer_contributions)),
                net_salary=Decimal(str(payslip.net_salary)),
                payable_days=Decimal(str(payslip.calculation_snapshot.get("payable_days", "0"))),
                status=payslip.status,
            )
            for payslip in payslips
        ]

    async def approval_preview(self, company_id: UUID, run_id: UUID) -> PayrollApprovalPreview:
        """The journal approving this run would hand to Accounting.

        Read-only. Lets somebody see — and reconcile — the accounting effect
        before committing to it.
        """
        run = await self._require_run(company_id, run_id)
        lines = await self._journal_lines(run)
        debits = sum((line.amount for line in lines if line.side == "DEBIT"), start=ZERO)
        credits = sum((line.amount for line in lines if line.side == "CREDIT"), start=ZERO)
        return PayrollApprovalPreview(
            run_id=run.id,
            run_number=run.run_number,
            currency_code=run.currency_code,
            total_debits=debits,
            total_credits=credits,
            balanced=debits == credits,
            lines=lines,
        )

    # ------------------------------------------------------------- internals
    async def _journal_lines(self, run: PayrollRun) -> list[PayrollJournalLine]:
        """Aggregate a run into balanced journal lines.

        HRM does not know what a journal entry is. It knows which GL account
        each salary component maps to, because that mapping is HR
        configuration, and it hands the Accounting module a pre-aggregated
        payload that `CreateJournalCommand` can consume directly.

        The arithmetic balances by construction:

            debits  = gross + employer_contributions
            credits = deductions + net + employer_contributions
                    = deductions + (gross - deductions) + employer
                    = gross + employer
        """
        totals = await self.uow.payslips.component_totals_for_run(run.id)
        lines: list[PayrollJournalLine] = []
        employer_total = ZERO

        for code, component_type, gl_account, amount in totals:
            kind = SalaryComponentType(component_type)
            if amount == 0:
                continue
            if kind is SalaryComponentType.EARNING:
                lines.append(
                    PayrollJournalLine(
                        # BASIC is not a configurable component and carries no
                        # account of its own; its expense account is configured.
                        gl_account_code=(
                            gl_account
                            if gl_account is not None
                            else (
                                settings.payroll_basic_salary_account
                                if code == BASIC_SALARY_CODE
                                else None
                            )
                        ),
                        side="DEBIT",
                        amount=amount,
                        component_code=code,
                        description=f"Payroll {run.run_number}: {code} (earning)",
                    )
                )
            elif kind is SalaryComponentType.DEDUCTION:
                lines.append(
                    PayrollJournalLine(
                        gl_account_code=gl_account,
                        side="CREDIT",
                        amount=amount,
                        component_code=code,
                        description=f"Payroll {run.run_number}: {code} (deduction)",
                    )
                )
            else:
                employer_total += amount
                lines.append(
                    PayrollJournalLine(
                        gl_account_code=gl_account,
                        side="DEBIT",
                        amount=amount,
                        component_code=code,
                        description=f"Payroll {run.run_number}: {code} (employer cost)",
                    )
                )

        net = Decimal(str(run.total_net))
        if net != 0:
            lines.append(
                PayrollJournalLine(
                    gl_account_code=settings.payroll_net_payable_account,
                    side="CREDIT",
                    amount=net,
                    component_code=NET_PAY_CODE,
                    description=f"Payroll {run.run_number}: net pay payable",
                )
            )
        if employer_total != 0:
            lines.append(
                PayrollJournalLine(
                    gl_account_code=settings.payroll_employer_liability_account,
                    side="CREDIT",
                    amount=quantize_currency(employer_total, 2),
                    component_code=EMPLOYER_LIABILITY_CODE,
                    description=f"Payroll {run.run_number}: employer contributions payable",
                )
            )
        return lines

    async def _mark_failed(
        self, company_id: UUID, run_id: UUID, reason: str, actor: ActorContext
    ) -> None:
        run = await self.uow.payroll_runs.get_scoped(company_id, run_id)
        if run is None:  # pragma: no cover - the run existed a moment ago
            return
        run.status = PayrollRunStatus.FAILED
        run.failure_reason = reason[:1000]
        self._audit(
            AuditAction.PAYROLL_FAILED,
            "PayrollRun",
            run.id,
            company_id,
            actor,
            new_data={"status": str(run.status), "failure_reason": run.failure_reason},
        )
        await self.uow.commit()

    async def _require_period(self, company_id: UUID, period_id: UUID) -> PayrollPeriod:
        row = await self.uow.payroll_periods.get_scoped(company_id, period_id)
        if row is None:
            raise PayrollPeriodNotFoundError(details={"period_id": str(period_id)})
        return row

    async def _require_period_locked(self, company_id: UUID, period_id: UUID) -> PayrollPeriod:
        row = await self.uow.payroll_periods.get_for_update(company_id, period_id)
        if row is None:
            raise PayrollPeriodNotFoundError(details={"period_id": str(period_id)})
        return row

    async def _require_run(self, company_id: UUID, run_id: UUID) -> PayrollRun:
        row = await self.uow.payroll_runs.get_scoped(company_id, run_id)
        if row is None:
            raise PayrollRunNotFoundError(details={"run_id": str(run_id)})
        return row

    async def _require_run_locked(self, company_id: UUID, run_id: UUID) -> PayrollRun:
        row = await self.uow.payroll_runs.get_for_update(company_id, run_id)
        if row is None:
            raise PayrollRunNotFoundError(details={"run_id": str(run_id)})
        return row

    def _audit(
        self,
        action: AuditAction,
        entity_type: str,
        entity_id: UUID,
        company_id: UUID,
        actor: ActorContext,
        *,
        previous_data: dict[str, Any] | None = None,
        new_data: dict[str, Any] | None = None,
    ) -> None:
        self.uow.audit.record(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            company_id=company_id,
            user_id=actor.user_id,
            previous_data=previous_data,
            new_data=new_data,
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )


__all__ = ["PayrollService"]
