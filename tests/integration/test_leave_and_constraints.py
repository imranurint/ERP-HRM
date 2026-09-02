"""Leave flow, balance projection, and the database constraints themselves.

The constraint tests deliberately bypass the services and insert directly, so
they prove the *database* holds the line. An application check alone loses the
race between two concurrent inserts; these constraints cannot.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.context import ActorContext
from app.application.leave.schemas import (
    BalanceAdjustmentRequest,
    LeaveRequestCreate,
)
from app.application.leave.service import LeaveService
from app.domain.employees.enums import ContractStatus, ContractType
from app.domain.leave.enums import LeaveRequestStatus, LeaveTransactionType
from app.domain.leave.exceptions import (
    LeaveBalanceInsufficientError,
    LeaveNotPendingError,
    LeaveOverlapError,
    SelfApprovalError,
)
from app.domain.leave.rules import replay
from app.domain.payroll.enums import SalaryStructureStatus
from app.infrastructure.database.models.employee import EmploymentContract
from app.infrastructure.database.models.leave import EmployeeLeavePolicy
from app.infrastructure.database.models.salary import EmployeeSalaryStructure
from app.infrastructure.database.unit_of_work import UnitOfWork
from app.infrastructure.outbox.models import OutboxEvent
from tests.factories import DemoData

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def leave_request(demo: DemoData, code: str = "ANNUAL", **kwargs: object) -> LeaveRequestCreate:
    defaults: dict[str, object] = {
        "leave_type_id": demo.leave_types[code].id,
        "start_date": date(2026, 3, 2),
        "end_date": date(2026, 3, 4),
        "submit": True,
    }
    defaults.update(kwargs)
    return LeaveRequestCreate(**defaults)  # type: ignore[arg-type]


class TestLeaveFlow:
    async def test_request_and_approve_consumes_balance(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service = LeaveService(UnitOfWork(session))
        requester = ActorContext(employee_id=demo.employee.id)
        approver = ActorContext(employee_id=demo.other_employee.id)

        created = await service.create_request(
            demo.company_id, leave_request(demo), demo.employee.id, requester
        )
        assert created.status is LeaveRequestStatus.PENDING
        assert created.total_days == Decimal("3.00")

        # Pending reserves but does not spend.
        before = await service.balances_for_employee(demo.company_id, demo.employee.id, 2026)
        annual_before = next(b for b in before if b.leave_code == "ANNUAL")
        assert annual_before.used == Decimal("0.00")
        assert annual_before.pending_days == Decimal("3.00")
        assert annual_before.available == Decimal("22.00")

        approved = await service.approve(demo.company_id, created.id, approver)
        assert approved.status is LeaveRequestStatus.APPROVED

        after = await service.balances_for_employee(demo.company_id, demo.employee.id, 2026)
        annual_after = next(b for b in after if b.leave_code == "ANNUAL")
        assert annual_after.used == Decimal("3.00")
        assert annual_after.closing_balance == Decimal("22.00")

    async def test_weekends_and_holidays_are_excluded_from_the_day_count(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """The factory seeds a holiday on 2026-03-17 (a Tuesday)."""
        service = LeaveService(UnitOfWork(session))
        created = await service.create_request(
            demo.company_id,
            leave_request(demo, start_date=date(2026, 3, 16), end_date=date(2026, 3, 18)),
            demo.employee.id,
            ActorContext(employee_id=demo.employee.id),
        )
        assert created.total_days == Decimal("2.00")

    async def test_rejection_leaves_the_balance_untouched(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """A pending request never consumed balance, so nothing to unwind."""
        service = LeaveService(UnitOfWork(session))
        created = await service.create_request(
            demo.company_id,
            leave_request(demo),
            demo.employee.id,
            ActorContext(employee_id=demo.employee.id),
        )
        await service.reject(
            demo.company_id,
            created.id,
            "Team is short-staffed",
            ActorContext(employee_id=demo.other_employee.id),
        )

        balances = await service.balances_for_employee(demo.company_id, demo.employee.id, 2026)
        annual = next(b for b in balances if b.leave_code == "ANNUAL")
        assert annual.used == Decimal("0.00")
        assert annual.pending_days == Decimal("0.00")

    async def test_cancelling_approved_leave_returns_the_days(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service = LeaveService(UnitOfWork(session))
        created = await service.create_request(
            demo.company_id,
            leave_request(demo, start_date=date(2026, 12, 1), end_date=date(2026, 12, 3)),
            demo.employee.id,
            ActorContext(employee_id=demo.employee.id),
        )
        await service.approve(
            demo.company_id, created.id, ActorContext(employee_id=demo.other_employee.id)
        )
        await service.cancel(
            demo.company_id, created.id, ActorContext(employee_id=demo.employee.id)
        )

        balances = await service.balances_for_employee(demo.company_id, demo.employee.id, 2026)
        annual = next(b for b in balances if b.leave_code == "ANNUAL")
        assert annual.used == Decimal("0.00")
        # Returned by reducing `used`, not by inflating `earned`.
        assert annual.earned == Decimal("25.00")

    async def test_a_leave_type_needing_no_approval_is_approved_on_submission(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """Otherwise it would sit PENDING forever with nobody to action it."""
        service = LeaveService(UnitOfWork(session))
        created = await service.create_request(
            demo.company_id,
            leave_request(demo, code="COMP", end_date=date(2026, 3, 3)),
            demo.employee.id,
            ActorContext(employee_id=demo.employee.id),
        )
        assert created.status is LeaveRequestStatus.APPROVED

    async def test_nobody_approves_their_own_leave(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service = LeaveService(UnitOfWork(session))
        actor = ActorContext(employee_id=demo.employee.id)
        created = await service.create_request(
            demo.company_id, leave_request(demo), demo.employee.id, actor
        )
        request_id = created.id
        with pytest.raises(SelfApprovalError):
            await service.approve(demo.company_id, request_id, actor)

    async def test_approving_twice_is_refused(self, session: AsyncSession, demo: DemoData) -> None:
        service = LeaveService(UnitOfWork(session))
        approver = ActorContext(employee_id=demo.other_employee.id)
        created = await service.create_request(
            demo.company_id,
            leave_request(demo),
            demo.employee.id,
            ActorContext(employee_id=demo.employee.id),
        )
        request_id = created.id
        await service.approve(demo.company_id, request_id, approver)
        with pytest.raises(LeaveNotPendingError):
            await service.approve(demo.company_id, request_id, approver)

    async def test_overlapping_leave_is_refused(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service = LeaveService(UnitOfWork(session))
        actor = ActorContext(employee_id=demo.employee.id)
        await service.create_request(demo.company_id, leave_request(demo), demo.employee.id, actor)

        with pytest.raises(LeaveOverlapError):
            await service.create_request(
                demo.company_id,
                leave_request(demo, start_date=date(2026, 3, 3), end_date=date(2026, 3, 5)),
                demo.employee.id,
                actor,
            )

    async def test_a_request_beyond_the_balance_is_refused(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service = LeaveService(UnitOfWork(session))
        with pytest.raises(LeaveBalanceInsufficientError):
            await service.create_request(
                demo.company_id,
                leave_request(demo, start_date=date(2026, 4, 1), end_date=date(2026, 12, 31)),
                demo.employee.id,
                ActorContext(employee_id=demo.employee.id),
            )

    async def test_approval_emits_an_event(self, session: AsyncSession, demo: DemoData) -> None:
        service = LeaveService(UnitOfWork(session))
        created = await service.create_request(
            demo.company_id,
            leave_request(demo),
            demo.employee.id,
            ActorContext(employee_id=demo.employee.id),
        )
        await service.approve(
            demo.company_id, created.id, ActorContext(employee_id=demo.other_employee.id)
        )
        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "LeaveApproved")
        )
        assert event is not None
        assert event.payload["total_days"] == "3.00"


class TestBalanceProjection:
    async def test_the_projection_matches_a_replay_of_its_transactions(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """The property `make rebuild-leave-balances` verifies in production."""
        service = LeaveService(UnitOfWork(session))
        created = await service.create_request(
            demo.company_id,
            leave_request(demo),
            demo.employee.id,
            ActorContext(employee_id=demo.employee.id),
        )
        await service.approve(
            demo.company_id, created.id, ActorContext(employee_id=demo.other_employee.id)
        )

        uow = UnitOfWork(session)
        leave_type_id = demo.leave_types["ANNUAL"].id
        transactions = await uow.leave_transactions.list_for_balance(
            demo.company_id, demo.employee.id, leave_type_id, 2026
        )
        rebuilt = replay([(t.transaction_type, Decimal(str(t.days))) for t in transactions])
        stored = await uow.leave_balances.snapshot(
            demo.company_id, demo.employee.id, leave_type_id, 2026
        )
        assert rebuilt == stored

    async def test_an_adjustment_moves_the_balance_and_records_a_reason(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        service = LeaveService(UnitOfWork(session))
        balance = await service.adjust_balance(
            demo.company_id,
            BalanceAdjustmentRequest(
                employee_id=demo.employee.id,
                leave_type_id=demo.leave_types["ANNUAL"].id,
                year=2026,
                days=Decimal("-2.00"),
                notes="Correcting a mis-keyed 2025 carry-forward",
            ),
            ActorContext(),
        )
        assert balance.adjustment == Decimal("-2.00")
        assert balance.closing_balance == Decimal("23.00")

    async def test_the_closing_balance_check_constraint_is_live(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """Try to write an arithmetically impossible balance directly."""
        from app.infrastructure.database.models.leave import LeaveBalance

        session.add(
            LeaveBalance(
                company_id=demo.company_id,
                employee_id=demo.employee.id,
                leave_type_id=demo.leave_types["SICK"].id,
                year=2099,
                opening_balance=Decimal("0"),
                earned=Decimal("10"),
                used=Decimal("2"),
                adjustment=Decimal("0"),
                closing_balance=Decimal("999"),  # wrong on purpose
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_transactions_are_recorded_for_every_movement(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        uow = UnitOfWork(session)
        rows = await uow.leave_transactions.list_for_balance(
            demo.company_id, demo.employee.id, demo.leave_types["ANNUAL"].id, 2026
        )
        assert len(rows) == 1
        assert rows[0].transaction_type is LeaveTransactionType.ENTITLEMENT


class TestDatabaseConstraints:
    """These bypass the services on purpose: they test PostgreSQL, not Python."""

    async def test_only_one_active_contract_per_employee(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        session.add(
            EmploymentContract(
                company_id=demo.company_id,
                employee_id=demo.employee.id,
                contract_number="CON-DUPLICATE",
                contract_type=ContractType.PERMANENT,
                start_date=date(2026, 6, 1),
                status=ContractStatus.ACTIVE,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_many_draft_contracts_are_fine(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """The index is partial — only ACTIVE rows are constrained."""
        for index in range(3):
            session.add(
                EmploymentContract(
                    company_id=demo.company_id,
                    employee_id=demo.employee.id,
                    contract_number=f"CON-DRAFT-{index}",
                    contract_type=ContractType.PERMANENT,
                    start_date=date(2026, 6, 1),
                    status=ContractStatus.DRAFT,
                )
            )
        await session.flush()
        await session.rollback()

    async def test_salary_structures_cannot_overlap(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """ "What was their salary on date X?" must have exactly one answer."""
        session.add(
            EmployeeSalaryStructure(
                company_id=demo.company_id,
                employee_id=demo.employee.id,
                effective_from=date(2026, 6, 1),
                currency_code="GBP",
                base_salary=Decimal("5000.00"),
                status=SalaryStructureStatus.ACTIVE,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_leave_policy_assignments_cannot_overlap(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        session.add(
            EmployeeLeavePolicy(
                company_id=demo.company_id,
                employee_id=demo.employee.id,
                leave_policy_id=demo.policy.id,
                effective_from=date(2026, 6, 1),
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_employee_number_is_unique_per_company(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        from app.domain.employees.enums import EmploymentStatus
        from app.infrastructure.database.models.employee import Employee

        session.add(
            Employee(
                company_id=demo.company_id,
                employee_number=demo.employee.employee_number,
                first_name="Clone",
                last_name="Person",
                work_email="clone@test.example",
                hire_date=date(2026, 2, 1),
                employment_status=EmploymentStatus.ACTIVE,
                is_active=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_is_active_must_match_employment_status(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        """The CHECK that stops headcount reports drifting from reality."""
        from app.domain.employees.enums import EmploymentStatus
        from app.infrastructure.database.models.employee import Employee

        session.add(
            Employee(
                company_id=demo.company_id,
                employee_number="EMP-BROKEN",
                first_name="Broken",
                last_name="Flags",
                work_email="broken@test.example",
                hire_date=date(2026, 2, 1),
                employment_status=EmploymentStatus.TERMINATED,
                termination_date=date(2026, 3, 1),
                is_active=True,  # contradicts TERMINATED
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_a_terminal_status_requires_a_termination_date(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        from app.domain.employees.enums import EmploymentStatus
        from app.infrastructure.database.models.employee import Employee

        session.add(
            Employee(
                company_id=demo.company_id,
                employee_number="EMP-NODATE",
                first_name="No",
                last_name="Date",
                work_email="nodate@test.example",
                hire_date=date(2026, 2, 1),
                employment_status=EmploymentStatus.RESIGNED,
                termination_date=None,
                is_active=False,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_a_payslip_net_must_equal_gross_minus_deductions(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        from app.domain.payroll.enums import PayrollPeriodStatus, PayrollRunStatus, PayslipStatus
        from app.infrastructure.database.models.payroll import PayrollPeriod, PayrollRun, Payslip

        period = PayrollPeriod(
            company_id=demo.company_id,
            period_name="constraint-test",
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 31),
            status=PayrollPeriodStatus.OPEN,
        )
        session.add(period)
        await session.flush()

        run = PayrollRun(
            company_id=demo.company_id,
            payroll_period_id=period.id,
            run_number="PR-CONSTRAINT",
            status=PayrollRunStatus.DRAFT,
            currency_code="GBP",
        )
        session.add(run)
        await session.flush()

        session.add(
            Payslip(
                company_id=demo.company_id,
                payroll_run_id=run.id,
                employee_id=demo.employee.id,
                gross_salary=Decimal("1000.00"),
                total_deductions=Decimal("100.00"),
                net_salary=Decimal("5000.00"),  # arithmetically impossible
                currency_code="GBP",
                status=PayslipStatus.DRAFT,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


class TestSequences:
    async def test_numbers_are_allocated_without_gaps_under_normal_use(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        from app.domain.shared.enums import DocumentType

        uow = UnitOfWork(session)
        first = await uow.sequences.allocate_number(demo.company_id, 2026, DocumentType.PAYROLL_RUN)
        second = await uow.sequences.allocate_number(
            demo.company_id, 2026, DocumentType.PAYROLL_RUN
        )
        assert first == "PR-2026-000001"
        assert second == "PR-2026-000002"

    async def test_sequences_are_independent_per_document_type(
        self, session: AsyncSession, demo: DemoData
    ) -> None:
        from app.domain.shared.enums import DocumentType

        uow = UnitOfWork(session)
        run = await uow.sequences.allocate_number(demo.company_id, 2026, DocumentType.PAYROLL_RUN)
        assert run.startswith("PR-")
        # Employees were already allocated by the factory, so this continues
        # that sequence rather than restarting it.
        employee = await uow.sequences.allocate_number(demo.company_id, 2026, DocumentType.EMPLOYEE)
        assert employee.startswith("EMP-")
        assert employee != "EMP-2026-000001"
