"""Payroll end to end, against real PostgreSQL.

Covers the whole run — create period, create run, calculate, approve, pay —
plus the immutability guards and the journal payload handed to Accounting.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.attendance.application.schemas import ManualAttendanceRequest
from app.modules.attendance.application.service import AttendanceService
from app.modules.attendance.domain.enums import AttendanceStatus
from app.modules.attendance.domain.exceptions import AttendanceLockedError
from app.modules.payroll.application.schemas import (
    PayrollCalculateRequest,
    PayrollPeriodCreate,
    PayrollRunCreate,
    VoidPayslipRequest,
)
from app.modules.payroll.application.service import PayrollService
from app.modules.payroll.domain.enums import PayrollPeriodStatus, PayrollRunStatus, PayslipStatus
from app.modules.payroll.domain.exceptions import (
    PayrollAlreadyApprovedError,
    PayrollAlreadyPaidError,
    PayrollNotApprovedError,
    PayrollNotCalculatedError,
    PayrollPeriodOverlapError,
    PayrollRunInProgressError,
)
from app.modules.payroll.infrastructure.models import Payslip
from app.shared.context import ActorContext
from app.shared.outbox.models import OutboxEvent
from app.shared.unit_of_work.unit_of_work import UnitOfWork
from tests.factories import DemoData

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MARCH = PayrollPeriodCreate(
    period_name="2026-03",
    start_date=date(2026, 3, 1),
    end_date=date(2026, 3, 31),
    pay_date=date(2026, 4, 5),
)


async def run_payroll(
    session: AsyncSession, demo: DemoData, actor: ActorContext | None = None
) -> tuple[PayrollService, object, object]:
    """Create a period and a calculated run. The common setup for these tests."""
    actor = actor or ActorContext()
    service = PayrollService(UnitOfWork(session))
    period = await service.create_period(demo.company_id, MARCH, actor)
    run = await service.create_run(
        demo.company_id, PayrollRunCreate(payroll_period_id=period.id), actor
    )
    run = await service.calculate(demo.company_id, run.id, PayrollCalculateRequest(), actor)
    return service, period, run


class TestPeriods:
    async def test_overlapping_periods_are_refused(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service = PayrollService(UnitOfWork(session))
        actor = ActorContext()
        await service.create_period(demo.company_id, MARCH, actor)

        with pytest.raises(PayrollPeriodOverlapError):
            await service.create_period(
                demo.company_id,
                PayrollPeriodCreate(
                    period_name="2026-03-overlap",
                    start_date=date(2026, 3, 15),
                    end_date=date(2026, 4, 15),
                ),
                actor,
            )

    async def test_the_gist_constraint_catches_what_the_check_would_miss(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """Insert straight past the service to prove the database itself holds
        the line — that is what protects against two concurrent inserts."""
        from sqlalchemy.exc import IntegrityError

        from app.modules.payroll.infrastructure.models import PayrollPeriod

        service = PayrollService(UnitOfWork(session))
        await service.create_period(demo.company_id, MARCH, ActorContext())

        session.add(
            PayrollPeriod(
                company_id=demo.company_id,
                period_name="sneaky",
                start_date=date(2026, 3, 20),
                end_date=date(2026, 4, 20),
                status=PayrollPeriodStatus.OPEN,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_adjacent_periods_are_allowed(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service = PayrollService(UnitOfWork(session))
        actor = ActorContext()
        await service.create_period(demo.company_id, MARCH, actor)
        april = await service.create_period(
            demo.company_id,
            PayrollPeriodCreate(
                period_name="2026-04",
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 30),
            ),
            actor,
        )
        assert april.status is PayrollPeriodStatus.OPEN


class TestCalculation:
    async def test_a_run_produces_one_payslip_per_employee(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        _, _, run = await run_payroll(session, demo)
        assert run.status is PayrollRunStatus.COMPLETED
        assert run.payslip_count == len(demo.employees)

        count = await session.scalar(
            select(func.count()).select_from(Payslip).where(Payslip.payroll_run_id == run.id)
        )
        assert count == len(demo.employees)

    async def test_run_totals_reconcile_with_the_payslips(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        _, _, run = await run_payroll(session, demo)
        totals = await session.execute(
            select(
                func.sum(Payslip.gross_salary),
                func.sum(Payslip.total_deductions),
                func.sum(Payslip.net_salary),
            ).where(Payslip.payroll_run_id == run.id)
        )
        gross, deductions, net = totals.one()
        assert Decimal(str(gross)) == run.total_gross
        assert Decimal(str(deductions)) == run.total_deductions
        assert Decimal(str(net)) == run.total_net

    async def test_net_equals_gross_minus_deductions_on_every_payslip(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service, _, run = await run_payroll(session, demo)
        for row in await service.uow.payslips.list_for_run(run.id):
            assert row.net_salary == row.gross_salary - row.total_deductions

    async def test_payslip_items_sum_to_the_payslip_totals(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        from app.modules.payroll.domain.enums import SalaryComponentType

        service, _, run = await run_payroll(session, demo)
        for payslip in await service.uow.payslips.list_for_run(run.id):
            earnings = sum(
                item.amount
                for item in payslip.items
                if item.component_type is SalaryComponentType.EARNING
            )
            assert earnings == payslip.gross_salary

    async def test_the_snapshot_is_stored(self, session: AsyncSession, demo: DemoData) -> None:
        """Golden Rule 6 — the payslip must be explainable years later."""
        service, _, run = await run_payroll(session, demo)
        payslip = (await service.uow.payslips.list_for_run(run.id))[0]
        snapshot = payslip.calculation_snapshot
        assert snapshot["contractual_base_salary"] == "4000.00"
        assert snapshot["run_number"] == run.run_number
        assert snapshot["period"]["name"] == "2026-03"
        assert "component_order" in snapshot

    async def test_component_names_are_copied_not_joined(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """Renaming a component must not rewrite an issued payslip."""
        service, _, run = await run_payroll(session, demo)
        payslip = (await service.uow.payslips.list_for_run(run.id))[0]
        housing = next(i for i in payslip.items if i.component_code == "HOUSING")
        assert housing.component_name == "Housing Allowance"

        demo.components["HOUSING"].name = "Accommodation Allowance"
        await session.commit()

        refreshed = await service.uow.payslips.get_scoped(demo.company_id, payslip.id)
        assert refreshed is not None
        item = next(i for i in refreshed.items if i.component_code == "HOUSING")
        assert item.component_name == "Housing Allowance"

    async def test_absence_prorates_pay(self, session: AsyncSession, demo: DemoData) -> None:
        actor = ActorContext()
        attendance = AttendanceService(UnitOfWork(session))
        await attendance.record_manual(
            demo.company_id,
            ManualAttendanceRequest(
                employee_id=demo.employee.id,
                attendance_date=date(2026, 3, 3),
                status=AttendanceStatus.ABSENT,
            ),
            actor,
        )

        service, _, run = await run_payroll(session, demo, actor)
        payslips = {p.employee_id: p for p in await service.uow.payslips.list_for_run(run.id)}
        absent = payslips[demo.employee.id]
        present = payslips[demo.other_employee.id]
        assert absent.gross_salary < present.gross_salary

    async def test_recalculation_is_deterministic(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """Golden Rule 4, proved through the database rather than in memory."""
        service, _, run = await run_payroll(session, demo)
        first_gross, first_net = run.total_gross, run.total_net

        again = await service.calculate(
            demo.company_id, run.id, PayrollCalculateRequest(), ActorContext()
        )
        assert again.total_gross == first_gross
        assert again.total_net == first_net
        assert again.payslip_count == run.payslip_count

    async def test_a_second_live_run_is_refused(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """Two runs on one period would double the payroll."""
        service, period, _ = await run_payroll(session, demo)
        with pytest.raises(PayrollRunInProgressError):
            await service.create_run(
                demo.company_id,
                PayrollRunCreate(payroll_period_id=period.id),
                ActorContext(),
            )


class TestApprovalAndPayment:
    async def test_the_happy_path(self, session: AsyncSession, demo: DemoData) -> None:
        service, period, run = await run_payroll(session, demo)

        approved = await service.approve(demo.company_id, run.id, ActorContext())
        assert approved.status is PayrollRunStatus.APPROVED
        assert approved.approved_at is not None

        paid = await service.pay(demo.company_id, run.id, ActorContext())
        assert paid.status is PayrollRunStatus.PAID
        assert paid.paid_at is not None

        refreshed_period = await service.get_period(demo.company_id, period.id)
        assert refreshed_period.status is PayrollPeriodStatus.PAID

    async def test_cannot_approve_a_draft_run(self, session: AsyncSession, demo: DemoData) -> None:
        service = PayrollService(UnitOfWork(session))
        actor = ActorContext()
        period = await service.create_period(demo.company_id, MARCH, actor)
        run = await service.create_run(
            demo.company_id, PayrollRunCreate(payroll_period_id=period.id), actor
        )
        with pytest.raises(PayrollNotCalculatedError):
            await service.approve(demo.company_id, run.id, actor)

    async def test_cannot_pay_before_approval(self, session: AsyncSession, demo: DemoData) -> None:
        service, _, run = await run_payroll(session, demo)
        with pytest.raises(PayrollNotApprovedError):
            await service.pay(demo.company_id, run.id, ActorContext())

    async def test_approved_payroll_cannot_be_recalculated(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """Golden Rule 2."""
        service, _, run = await run_payroll(session, demo)
        run_id = run.id  # captured before a call that will roll back
        await service.approve(demo.company_id, run_id, ActorContext())

        with pytest.raises(PayrollAlreadyApprovedError):
            await service.calculate(
                demo.company_id, run_id, PayrollCalculateRequest(), ActorContext()
            )

    async def test_paid_payroll_cannot_be_recalculated(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """Golden Rule 3."""
        service, _, run = await run_payroll(session, demo)
        run_id = run.id
        await service.approve(demo.company_id, run_id, ActorContext())
        await service.pay(demo.company_id, run_id, ActorContext())

        with pytest.raises(PayrollAlreadyPaidError):
            await service.calculate(
                demo.company_id, run_id, PayrollCalculateRequest(), ActorContext()
            )

    async def test_payslips_follow_the_run_status(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service, _, run = await run_payroll(session, demo)
        await service.approve(demo.company_id, run.id, ActorContext())
        for payslip in await service.uow.payslips.list_for_run(run.id):
            assert payslip.status is PayslipStatus.APPROVED

        await service.pay(demo.company_id, run.id, ActorContext())
        for payslip in await service.uow.payslips.list_for_run(run.id):
            assert payslip.status is PayslipStatus.PAID

    async def test_an_adjustment_run_corrects_an_approved_one(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """Golden Rule 7: correct by adding, never by editing."""
        service, period, run = await run_payroll(session, demo)
        await service.approve(demo.company_id, run.id, ActorContext())

        adjustment = await service.create_run(
            demo.company_id,
            PayrollRunCreate(payroll_period_id=period.id, adjusts_run_id=run.id),
            ActorContext(),
        )
        assert adjustment.adjusts_run_id == run.id
        # The original is untouched and still visible.
        original = await service.get_run(demo.company_id, run.id)
        assert original.status is PayrollRunStatus.APPROVED


class TestPayslipVoiding:
    async def test_voiding_excludes_a_payslip_from_the_totals(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service, _, run = await run_payroll(session, demo)
        payslips = await service.uow.payslips.list_for_run(run.id)
        target = payslips[0]
        before = run.total_gross

        await service.void_payslip(
            demo.company_id,
            target.id,
            VoidPayslipRequest(reason="Duplicate record"),
            ActorContext(),
        )
        after = await service.get_run(demo.company_id, run.id)
        assert after.payslip_count == run.payslip_count - 1
        assert after.total_gross < before

    async def test_a_paid_payslip_cannot_be_voided(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service, _, run = await run_payroll(session, demo)
        payslip_id = (await service.uow.payslips.list_for_run(run.id))[0].id
        await service.approve(demo.company_id, run.id, ActorContext())
        await service.pay(demo.company_id, run.id, ActorContext())

        with pytest.raises(PayrollAlreadyPaidError):
            await service.void_payslip(
                demo.company_id, payslip_id, VoidPayslipRequest(reason="oops"), ActorContext()
            )


class TestAccountingHandoff:
    async def test_the_journal_balances(self, session: AsyncSession, demo: DemoData) -> None:
        """debits == credits, by construction."""
        service, _, run = await run_payroll(session, demo)
        preview = await service.approval_preview(demo.company_id, run.id)
        assert preview.balanced is True
        assert preview.total_debits == preview.total_credits
        assert preview.total_debits > 0

    async def test_journal_lines_carry_gl_account_codes(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service, _, run = await run_payroll(session, demo)
        preview = await service.approval_preview(demo.company_id, run.id)
        codes = {line.gl_account_code for line in preview.lines}
        assert "6010" in codes  # housing, an expense
        assert "2200" in codes  # pension, a liability
        assert "2100" in codes  # net pay payable, from configuration

    async def test_approval_emits_a_payroll_approved_event(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service, _, run = await run_payroll(session, demo)
        await service.approve(demo.company_id, run.id, ActorContext())

        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "PayrollApproved")
        )
        assert event is not None
        assert event.payload["run_number"] == run.run_number
        assert len(event.payload["journal_lines"]) > 0
        assert event.status.value == "PENDING"

    async def test_payment_emits_a_payroll_paid_event(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service, _, run = await run_payroll(session, demo)
        await service.approve(demo.company_id, run.id, ActorContext())
        await service.pay(demo.company_id, run.id, ActorContext())

        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "PayrollPaid")
        )
        assert event is not None


class TestSourceDataLocking:
    async def test_attendance_freezes_once_payroll_is_approved(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """Editing the inputs would silently invalidate a signed-off payslip."""
        service, _, run = await run_payroll(session, demo)
        await service.approve(demo.company_id, run.id, ActorContext())

        attendance = AttendanceService(UnitOfWork(session))
        with pytest.raises(AttendanceLockedError):
            await attendance.record_manual(
                demo.company_id,
                ManualAttendanceRequest(
                    employee_id=demo.employee.id,
                    attendance_date=date(2026, 3, 10),
                    status=AttendanceStatus.ABSENT,
                ),
                ActorContext(),
            )

    async def test_attendance_is_editable_while_the_period_is_open(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service = PayrollService(UnitOfWork(session))
        await service.create_period(demo.company_id, MARCH, ActorContext())

        attendance = AttendanceService(UnitOfWork(session))
        record = await attendance.record_manual(
            demo.company_id,
            ManualAttendanceRequest(
                employee_id=demo.employee.id,
                attendance_date=date(2026, 3, 10),
                status=AttendanceStatus.ABSENT,
            ),
            ActorContext(),
        )
        assert record.status is AttendanceStatus.ABSENT
