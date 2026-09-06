"""Payroll repositories.

The `_for_update` methods are the concurrency backbone of payroll. A run and
its period are both row-locked for the duration of a calculate/approve/pay
transaction, so two operators cannot approve the same run at once and a period
cannot be closed halfway through a run against it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

from app.core.pagination import PageParams
from app.modules.payroll.domain.enums import PayrollPeriodStatus, PayrollRunStatus, PayslipStatus
from app.modules.payroll.infrastructure.models import (
    PayrollPeriod,
    PayrollRun,
    Payslip,
    PayslipItem,
)
from app.shared.database.repository import BaseRepository

#: Runs that still hold a claim on their period.
LIVE_RUN_STATUSES = (
    PayrollRunStatus.DRAFT,
    PayrollRunStatus.PROCESSING,
    PayrollRunStatus.COMPLETED,
)


class PayrollPeriodRepository(BaseRepository[PayrollPeriod]):
    model = PayrollPeriod

    async def get_scoped(self, company_id: UUID, period_id: UUID) -> PayrollPeriod | None:
        stmt = select(PayrollPeriod).where(
            PayrollPeriod.id == period_id, PayrollPeriod.company_id == company_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_for_update(self, company_id: UUID, period_id: UUID) -> PayrollPeriod | None:
        """Lock the period row for the whole payroll transaction.

        This is the direct analogue of the Accounting module locking a fiscal
        period before posting: it makes "the period was open when we started"
        stay true until the transaction commits.
        """
        stmt = (
            select(PayrollPeriod)
            .where(PayrollPeriod.id == period_id, PayrollPeriod.company_id == company_id)
            .with_for_update()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_name(self, company_id: UUID, period_name: str) -> PayrollPeriod | None:
        stmt = select(PayrollPeriod).where(
            PayrollPeriod.company_id == company_id, PayrollPeriod.period_name == period_name.strip()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def covering(self, company_id: UUID, day: date) -> PayrollPeriod | None:
        """The period a given date falls in. Guards attendance and leave edits."""
        stmt = select(PayrollPeriod).where(
            PayrollPeriod.company_id == company_id,
            PayrollPeriod.start_date <= day,
            PayrollPeriod.end_date >= day,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def existing_ranges(
        self, company_id: UUID, *, exclude_id: UUID | None = None
    ) -> list[tuple[date, date]]:
        stmt = select(PayrollPeriod.start_date, PayrollPeriod.end_date).where(
            PayrollPeriod.company_id == company_id
        )
        if exclude_id is not None:
            stmt = stmt.where(PayrollPeriod.id != exclude_id)
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def list_paginated(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        status: PayrollPeriodStatus | None = None,
    ) -> tuple[Sequence[PayrollPeriod], int]:
        stmt = select(PayrollPeriod).where(PayrollPeriod.company_id == company_id)
        if status is not None:
            stmt = stmt.where(PayrollPeriod.status == status)
        total = await self.count(stmt)
        stmt = (
            stmt.order_by(PayrollPeriod.start_date.desc()).offset(params.offset).limit(params.limit)
        )
        return (await self.session.execute(stmt)).scalars().all(), total


class PayrollRunRepository(BaseRepository[PayrollRun]):
    model = PayrollRun

    async def get_scoped(self, company_id: UUID, run_id: UUID) -> PayrollRun | None:
        stmt = (
            select(PayrollRun)
            .where(PayrollRun.id == run_id, PayrollRun.company_id == company_id)
            .options(joinedload(PayrollRun.period))
        )
        return (await self.session.execute(stmt)).unique().scalar_one_or_none()

    async def get_for_update(self, company_id: UUID, run_id: UUID) -> PayrollRun | None:
        """Row-locked run. Every state transition takes this first."""
        stmt = (
            select(PayrollRun)
            .where(PayrollRun.id == run_id, PayrollRun.company_id == company_id)
            .with_for_update(of=PayrollRun)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def live_run_exists(self, period_id: UUID, *, exclude_id: UUID | None = None) -> bool:
        """Is another run already working on this period?

        Two concurrent runs would each produce a full set of payslips for the
        same period and double the payroll.
        """
        stmt = (
            select(func.count())
            .select_from(PayrollRun)
            .where(
                PayrollRun.payroll_period_id == period_id,
                PayrollRun.status.in_(LIVE_RUN_STATUSES),
            )
        )
        if exclude_id is not None:
            stmt = stmt.where(PayrollRun.id != exclude_id)
        return bool(await self.session.scalar(stmt))

    async def list_paginated(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        period_id: UUID | None = None,
        status: PayrollRunStatus | None = None,
    ) -> tuple[Sequence[PayrollRun], int]:
        stmt = select(PayrollRun).where(PayrollRun.company_id == company_id)
        if period_id is not None:
            stmt = stmt.where(PayrollRun.payroll_period_id == period_id)
        if status is not None:
            stmt = stmt.where(PayrollRun.status == status)
        total = await self.count(stmt)
        stmt = (
            stmt.order_by(PayrollRun.created_at.desc())
            .offset(params.offset)
            .limit(params.limit)
            .options(joinedload(PayrollRun.period))
        )
        rows = (await self.session.execute(stmt)).unique().scalars().all()
        return rows, total


class PayslipRepository(BaseRepository[Payslip]):
    model = Payslip

    async def get_scoped(self, company_id: UUID, payslip_id: UUID) -> Payslip | None:
        stmt = (
            select(Payslip)
            .where(Payslip.id == payslip_id, Payslip.company_id == company_id)
            .options(selectinload(Payslip.items))
        )
        return (await self.session.execute(stmt)).unique().scalar_one_or_none()

    async def list_for_run(self, run_id: UUID) -> Sequence[Payslip]:
        stmt = (
            select(Payslip)
            .where(Payslip.payroll_run_id == run_id)
            .order_by(Payslip.employee_id)
            .options(selectinload(Payslip.items))
        )
        return (await self.session.execute(stmt)).unique().scalars().all()

    async def delete_for_run(self, run_id: UUID) -> int:
        """Clear a run's payslips before recalculating.

        Only ever reached after `ensure_run_mutable` has passed, so this can
        never touch an approved or paid run.
        """
        rows = (
            (await self.session.execute(select(Payslip).where(Payslip.payroll_run_id == run_id)))
            .scalars()
            .all()
        )
        for row in rows:
            await self.session.delete(row)
        return len(rows)

    async def list_paginated(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        employee_id: UUID | None = None,
        run_id: UUID | None = None,
        status: PayslipStatus | None = None,
    ) -> tuple[Sequence[Payslip], int]:
        stmt = select(Payslip).where(Payslip.company_id == company_id)
        if employee_id is not None:
            stmt = stmt.where(Payslip.employee_id == employee_id)
        if run_id is not None:
            stmt = stmt.where(Payslip.payroll_run_id == run_id)
        if status is not None:
            stmt = stmt.where(Payslip.status == status)
        total = await self.count(stmt)
        stmt = stmt.order_by(Payslip.created_at.desc()).offset(params.offset).limit(params.limit)
        return (await self.session.execute(stmt)).scalars().all(), total

    async def set_status_for_run(self, run_id: UUID, status: PayslipStatus) -> int:
        """Cascade a run-level transition to its payslips.

        Voided payslips are skipped: a payslip voided before approval must not
        be resurrected by approving the run.
        """
        rows = (
            (
                await self.session.execute(
                    select(Payslip).where(
                        Payslip.payroll_run_id == run_id, Payslip.status != PayslipStatus.VOID
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.status = status
        return len(rows)

    async def component_totals_for_run(
        self, run_id: UUID
    ) -> Sequence[tuple[str, str, str | None, Decimal]]:
        """Per-component totals across a run.

        This is what becomes the journal payload on `PayrollApproved`, and it
        is also the Salary Component Report. Aggregated in the database rather
        than in Python so a thousand-payslip run stays one query.
        """
        stmt = (
            select(
                PayslipItem.component_code,
                PayslipItem.component_type,
                PayslipItem.gl_account_code,
                func.sum(PayslipItem.amount),
            )
            .join(Payslip, Payslip.id == PayslipItem.payslip_id)
            .where(Payslip.payroll_run_id == run_id, Payslip.status != PayslipStatus.VOID)
            .group_by(
                PayslipItem.component_code,
                PayslipItem.component_type,
                PayslipItem.gl_account_code,
            )
            .order_by(PayslipItem.component_code)
        )
        return [
            (str(row[0]), str(row[1]), row[2], Decimal(str(row[3])))
            for row in (await self.session.execute(stmt)).all()
        ]

    async def run_totals(self, run_id: UUID) -> tuple[int, Decimal, Decimal, Decimal, Decimal]:
        """`(count, gross, deductions, employer, net)` for the non-void payslips."""
        stmt = select(
            func.count(),
            func.coalesce(func.sum(Payslip.gross_salary), 0),
            func.coalesce(func.sum(Payslip.total_deductions), 0),
            func.coalesce(func.sum(Payslip.employer_contributions), 0),
            func.coalesce(func.sum(Payslip.net_salary), 0),
        ).where(Payslip.payroll_run_id == run_id, Payslip.status != PayslipStatus.VOID)
        row = (await self.session.execute(stmt)).one()
        return (
            int(row[0]),
            Decimal(str(row[1])),
            Decimal(str(row[2])),
            Decimal(str(row[3])),
            Decimal(str(row[4])),
        )
