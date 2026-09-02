"""Leave repositories.

`LeaveBalanceRepository.apply_transactions` is the only write path into
`leave_balances`, and it is an atomic upsert. A read-modify-write would lose
updates whenever two approvals touched the same employee and leave type
concurrently, so it is never used here — the same rule the Accounting module
applies to `ledger_balances`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import bindparam, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload

from app.core.pagination import PageParams
from app.domain.leave.enums import LeaveRequestStatus, LeaveTransactionType
from app.domain.leave.rules import ZERO_DAYS, LeaveBalanceSnapshot, quantize_days
from app.infrastructure.database.models.leave import (
    EmployeeLeavePolicy,
    LeaveBalance,
    LeavePolicy,
    LeavePolicyRule,
    LeaveRequest,
    LeaveTransaction,
    LeaveType,
)
from app.infrastructure.database.repositories.base import BaseRepository

#: Statuses that still occupy the calendar and still reserve balance.
OPEN_STATUSES = (LeaveRequestStatus.DRAFT, LeaveRequestStatus.PENDING)

_CONFLICT_COLUMNS = ["company_id", "employee_id", "leave_type_id", "year"]


class LeaveTypeRepository(BaseRepository[LeaveType]):
    model = LeaveType

    async def get_scoped(self, company_id: UUID, leave_type_id: UUID) -> LeaveType | None:
        stmt = select(LeaveType).where(
            LeaveType.id == leave_type_id, LeaveType.company_id == company_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_code(self, company_id: UUID, code: str) -> LeaveType | None:
        stmt = select(LeaveType).where(
            LeaveType.company_id == company_id, LeaveType.leave_code == code.strip().upper()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_company(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> tuple[Sequence[LeaveType], int]:
        stmt = select(LeaveType).where(LeaveType.company_id == company_id)
        if is_active is not None:
            stmt = stmt.where(LeaveType.is_active == is_active)
        total = await self.count(stmt)
        stmt = stmt.order_by(LeaveType.leave_code).offset(params.offset).limit(params.limit)
        return (await self.session.execute(stmt)).scalars().all(), total


class LeavePolicyRepository(BaseRepository[LeavePolicy]):
    model = LeavePolicy

    async def get_scoped(self, company_id: UUID, policy_id: UUID) -> LeavePolicy | None:
        stmt = select(LeavePolicy).where(
            LeavePolicy.id == policy_id, LeavePolicy.company_id == company_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_company(
        self, company_id: UUID, params: PageParams
    ) -> tuple[Sequence[LeavePolicy], int]:
        stmt = select(LeavePolicy).where(LeavePolicy.company_id == company_id)
        total = await self.count(stmt)
        stmt = stmt.order_by(LeavePolicy.name).offset(params.offset).limit(params.limit)
        return (await self.session.execute(stmt)).scalars().all(), total

    async def rule_for(self, policy_id: UUID, leave_type_id: UUID) -> LeavePolicyRule | None:
        stmt = select(LeavePolicyRule).where(
            LeavePolicyRule.policy_id == policy_id,
            LeavePolicyRule.leave_type_id == leave_type_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def effective_policy(self, employee_id: UUID, as_of: date) -> EmployeeLeavePolicy | None:
        stmt = (
            select(EmployeeLeavePolicy)
            .where(
                EmployeeLeavePolicy.employee_id == employee_id,
                EmployeeLeavePolicy.effective_from <= as_of,
                or_(
                    EmployeeLeavePolicy.effective_to.is_(None),
                    EmployeeLeavePolicy.effective_to >= as_of,
                ),
            )
            .options(joinedload(EmployeeLeavePolicy.policy).selectinload(LeavePolicy.rules))
        )
        return (await self.session.execute(stmt)).unique().scalar_one_or_none()

    async def assignment_ranges(
        self, employee_id: UUID, *, exclude_id: UUID | None = None
    ) -> list[tuple[date, date | None]]:
        stmt = select(EmployeeLeavePolicy.effective_from, EmployeeLeavePolicy.effective_to).where(
            EmployeeLeavePolicy.employee_id == employee_id
        )
        if exclude_id is not None:
            stmt = stmt.where(EmployeeLeavePolicy.id != exclude_id)
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]


class LeaveRequestRepository(BaseRepository[LeaveRequest]):
    model = LeaveRequest

    async def get_scoped(self, company_id: UUID, request_id: UUID) -> LeaveRequest | None:
        stmt = (
            select(LeaveRequest)
            .where(LeaveRequest.id == request_id, LeaveRequest.company_id == company_id)
            .options(joinedload(LeaveRequest.leave_type))
        )
        return (await self.session.execute(stmt)).unique().scalar_one_or_none()

    async def get_for_update(self, company_id: UUID, request_id: UUID) -> LeaveRequest | None:
        """Row-locked, for approve / reject / cancel.

        All three read the current status and then write a new one. Without
        the lock, two approvers could both read PENDING and both consume the
        balance.
        """
        stmt = (
            select(LeaveRequest)
            .where(LeaveRequest.id == request_id, LeaveRequest.company_id == company_id)
            .with_for_update(of=LeaveRequest)
        )
        request = (await self.session.execute(stmt)).scalar_one_or_none()
        if request is not None:
            # Load the type separately: FOR UPDATE and an outer join do not mix.
            await self.session.refresh(request, ["leave_type"])
        return request

    async def overlapping_ranges(
        self,
        employee_id: UUID,
        *,
        exclude_id: UUID | None = None,
    ) -> list[tuple[date, date]]:
        """Ranges already blocked by open or approved leave."""
        stmt = select(LeaveRequest.start_date, LeaveRequest.end_date).where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status.in_([*OPEN_STATUSES, LeaveRequestStatus.APPROVED]),
        )
        if exclude_id is not None:
            stmt = stmt.where(LeaveRequest.id != exclude_id)
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def pending_days(self, employee_id: UUID, leave_type_id: UUID, year: int) -> Decimal:
        """Days held by requests awaiting a decision.

        Subtracted from the available balance so two same-morning requests
        cannot both be approved against one remaining day.
        """
        stmt = select(func.coalesce(func.sum(LeaveRequest.total_days), 0)).where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.leave_type_id == leave_type_id,
            LeaveRequest.status.in_(OPEN_STATUSES),
            func.extract("year", LeaveRequest.start_date) == year,
        )
        return quantize_days(Decimal(str(await self.session.scalar(stmt) or 0)))

    async def used_days_in_year(self, employee_id: UUID, leave_type_id: UUID, year: int) -> Decimal:
        stmt = select(func.coalesce(func.sum(LeaveRequest.total_days), 0)).where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.leave_type_id == leave_type_id,
            LeaveRequest.status == LeaveRequestStatus.APPROVED,
            func.extract("year", LeaveRequest.start_date) == year,
        )
        return quantize_days(Decimal(str(await self.session.scalar(stmt) or 0)))

    async def list_paginated(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        employee_id: UUID | None = None,
        leave_type_id: UUID | None = None,
        status: LeaveRequestStatus | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[Sequence[LeaveRequest], int]:
        stmt = select(LeaveRequest).where(LeaveRequest.company_id == company_id)
        if employee_id is not None:
            stmt = stmt.where(LeaveRequest.employee_id == employee_id)
        if leave_type_id is not None:
            stmt = stmt.where(LeaveRequest.leave_type_id == leave_type_id)
        if status is not None:
            stmt = stmt.where(LeaveRequest.status == status)
        if start_date is not None:
            stmt = stmt.where(LeaveRequest.end_date >= start_date)
        if end_date is not None:
            stmt = stmt.where(LeaveRequest.start_date <= end_date)

        total = await self.count(stmt)
        stmt = (
            stmt.order_by(LeaveRequest.start_date.desc())
            .offset(params.offset)
            .limit(params.limit)
            .options(joinedload(LeaveRequest.leave_type))
        )
        rows = (await self.session.execute(stmt)).unique().scalars().all()
        return rows, total

    async def approved_in_range(
        self,
        company_id: UUID,
        employee_ids: Sequence[UUID],
        start_date: date,
        end_date: date,
    ) -> Sequence[LeaveRequest]:
        """Approved leave overlapping a payroll period — one query for the run."""
        if not employee_ids:
            return []
        stmt = (
            select(LeaveRequest)
            .where(
                LeaveRequest.company_id == company_id,
                LeaveRequest.employee_id.in_(list(employee_ids)),
                LeaveRequest.status == LeaveRequestStatus.APPROVED,
                LeaveRequest.end_date >= start_date,
                LeaveRequest.start_date <= end_date,
            )
            .options(joinedload(LeaveRequest.leave_type))
        )
        return (await self.session.execute(stmt)).unique().scalars().all()


class LeaveTransactionRepository(BaseRepository[LeaveTransaction]):
    model = LeaveTransaction

    async def list_for_balance(
        self, company_id: UUID, employee_id: UUID, leave_type_id: UUID, year: int
    ) -> Sequence[LeaveTransaction]:
        stmt = (
            select(LeaveTransaction)
            .where(
                LeaveTransaction.company_id == company_id,
                LeaveTransaction.employee_id == employee_id,
                LeaveTransaction.leave_type_id == leave_type_id,
                LeaveTransaction.year == year,
            )
            .order_by(LeaveTransaction.created_at, LeaveTransaction.id)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def balance_keys(self, company_id: UUID) -> Sequence[tuple[UUID, UUID, int]]:
        """Every (employee, leave type, year) that has movements. Used by rebuild."""
        stmt = (
            select(
                LeaveTransaction.employee_id,
                LeaveTransaction.leave_type_id,
                LeaveTransaction.year,
            )
            .where(LeaveTransaction.company_id == company_id)
            .distinct()
        )
        return [(row[0], row[1], int(row[2])) for row in (await self.session.execute(stmt)).all()]

    async def all_for_company(
        self, company_id: UUID
    ) -> Sequence[tuple[UUID, UUID, int, LeaveTransactionType, Decimal]]:
        stmt = (
            select(
                LeaveTransaction.employee_id,
                LeaveTransaction.leave_type_id,
                LeaveTransaction.year,
                LeaveTransaction.transaction_type,
                LeaveTransaction.days,
            )
            .where(LeaveTransaction.company_id == company_id)
            .order_by(LeaveTransaction.created_at, LeaveTransaction.id)
        )
        return [
            (row[0], row[1], int(row[2]), LeaveTransactionType(row[3]), Decimal(str(row[4])))
            for row in (await self.session.execute(stmt)).all()
        ]


class LeaveBalanceRepository(BaseRepository[LeaveBalance]):
    model = LeaveBalance

    async def get(  # type: ignore[override]
        self, company_id: UUID, employee_id: UUID, leave_type_id: UUID, year: int
    ) -> LeaveBalance | None:
        stmt = select(LeaveBalance).where(
            LeaveBalance.company_id == company_id,
            LeaveBalance.employee_id == employee_id,
            LeaveBalance.leave_type_id == leave_type_id,
            LeaveBalance.year == year,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def snapshot(
        self, company_id: UUID, employee_id: UUID, leave_type_id: UUID, year: int
    ) -> LeaveBalanceSnapshot:
        """The balance as a domain value. Missing rows read as all-zero."""
        row = await self.get(company_id, employee_id, leave_type_id, year)
        if row is None:
            return LeaveBalanceSnapshot(ZERO_DAYS, ZERO_DAYS, ZERO_DAYS, ZERO_DAYS)
        return LeaveBalanceSnapshot(
            opening_balance=Decimal(str(row.opening_balance)),
            earned=Decimal(str(row.earned)),
            used=Decimal(str(row.used)),
            adjustment=Decimal(str(row.adjustment)),
        )

    async def apply_movement(
        self,
        *,
        company_id: UUID,
        employee_id: UUID,
        leave_type_id: UUID,
        year: int,
        opening_delta: Decimal = ZERO_DAYS,
        earned_delta: Decimal = ZERO_DAYS,
        used_delta: Decimal = ZERO_DAYS,
        adjustment_delta: Decimal = ZERO_DAYS,
    ) -> None:
        """Fold a movement into the projection atomically.

        `closing_balance` is recomputed from the *post-increment* totals, so
        it can never drift from its components — the CHECK constraint on the
        table verifies that on every write.

        **Why the INSERT values are clamped.** PostgreSQL evaluates CHECK
        constraints against the tuple proposed for insertion, *before* the
        `ON CONFLICT` arbiter decides to update instead. A cancellation
        carries a negative `used` delta, which would trip the
        `earned >= 0 AND used >= 0` check on that proposed row even though the
        row it actually updates ends up perfectly valid. So the INSERT branch
        clamps to zero and the DO UPDATE branch applies the true signed delta
        through its own bound parameters rather than through `excluded`.

        Clamping is safe rather than merely convenient: a negative delta can
        only arise from cancelling leave that was previously consumed, which
        means the row is guaranteed to already exist and the INSERT branch is
        guaranteed not to be the one taken.
        """
        deltas = {
            "opening_balance": opening_delta,
            "earned": earned_delta,
            "used": used_delta,
            "adjustment": adjustment_delta,
        }
        insert_values = {name: max(value, ZERO_DAYS) for name, value in deltas.items()}

        stmt = pg_insert(LeaveBalance).values(
            company_id=company_id,
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            year=year,
            **insert_values,
            closing_balance=(
                insert_values["opening_balance"]
                + insert_values["earned"]
                - insert_values["used"]
                + insert_values["adjustment"]
            ),
        )

        # Bound parameters, not `excluded`: `excluded` is the clamped INSERT
        # row, and the update needs the real signed delta.
        new_opening = LeaveBalance.opening_balance + bindparam("d_opening", opening_delta)
        new_earned = LeaveBalance.earned + bindparam("d_earned", earned_delta)
        new_used = LeaveBalance.used + bindparam("d_used", used_delta)
        new_adjustment = LeaveBalance.adjustment + bindparam("d_adjustment", adjustment_delta)

        stmt = stmt.on_conflict_do_update(
            index_elements=_CONFLICT_COLUMNS,
            set_={
                "opening_balance": new_opening,
                "earned": new_earned,
                "used": new_used,
                "adjustment": new_adjustment,
                "closing_balance": new_opening + new_earned - new_used + new_adjustment,
                "updated_at": text("now()"),
            },
        )
        await self.session.execute(stmt)

    async def apply_snapshot(
        self,
        *,
        company_id: UUID,
        employee_id: UUID,
        leave_type_id: UUID,
        year: int,
        snapshot: LeaveBalanceSnapshot,
    ) -> None:
        """Overwrite a balance with an absolute snapshot. Rebuild only."""
        stmt = pg_insert(LeaveBalance).values(
            company_id=company_id,
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            year=year,
            opening_balance=snapshot.opening_balance,
            earned=snapshot.earned,
            used=snapshot.used,
            adjustment=snapshot.adjustment,
            closing_balance=snapshot.closing_balance,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=_CONFLICT_COLUMNS,
            set_={
                "opening_balance": stmt.excluded.opening_balance,
                "earned": stmt.excluded.earned,
                "used": stmt.excluded.used,
                "adjustment": stmt.excluded.adjustment,
                "closing_balance": stmt.excluded.closing_balance,
                "updated_at": text("now()"),
            },
        )
        await self.session.execute(stmt)

    async def list_for_employee(
        self, company_id: UUID, employee_id: UUID, year: int
    ) -> Sequence[LeaveBalance]:
        stmt = select(LeaveBalance).where(
            LeaveBalance.company_id == company_id,
            LeaveBalance.employee_id == employee_id,
            LeaveBalance.year == year,
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def list_for_company(
        self,
        company_id: UUID,
        year: int,
        *,
        leave_type_id: UUID | None = None,
        employee_ids: Sequence[UUID] | None = None,
    ) -> Sequence[LeaveBalance]:
        stmt = select(LeaveBalance).where(
            LeaveBalance.company_id == company_id, LeaveBalance.year == year
        )
        if leave_type_id is not None:
            stmt = stmt.where(LeaveBalance.leave_type_id == leave_type_id)
        if employee_ids:
            stmt = stmt.where(LeaveBalance.employee_id.in_(list(employee_ids)))
        return (await self.session.execute(stmt)).scalars().all()

    async def list_for_company_all_years(self, company_id: UUID) -> Sequence[LeaveBalance]:
        """Every balance row for a company. Used by the rebuild's verify pass."""
        stmt = select(LeaveBalance).where(LeaveBalance.company_id == company_id)
        return (await self.session.execute(stmt)).scalars().all()

    async def clear_company(self, company_id: UUID) -> int:
        """Drop the projection for a company. Only used by the rebuild script."""
        result = await self.session.execute(
            delete(LeaveBalance).where(LeaveBalance.company_id == company_id)
        )
        return int(getattr(result, "rowcount", 0) or 0)
