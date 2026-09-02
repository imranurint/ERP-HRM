"""Leave service.

Balance handling is the interesting part. Two rules make it correct:

1. **Only APPROVED leave consumes balance.** A pending request is *checked*
   against the balance but never writes a transaction, so a rejection needs no
   unwinding. The reservation is enforced by counting open requests, which is
   what stops two same-morning requests both being approved against one
   remaining day.

2. **Every balance change is an append-only transaction first, a projection
   update second.** `leave_balances` is derived and fully rebuildable from
   `leave_transactions` — `make rebuild-leave-balances` proves it.

Approve, reject and cancel all take a row lock on the request before reading
its status, so two approvers cannot both win.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.application.context import ActorContext
from app.application.leave.schemas import (
    AssignPolicyRequest,
    BalanceAdjustmentRequest,
    EmployeeLeavePolicyRead,
    LeaveBalanceRead,
    LeavePolicyCreate,
    LeavePolicyRead,
    LeaveRequestCreate,
    LeaveRequestRead,
    LeaveTransactionRead,
    LeaveTypeCreate,
    LeaveTypeRead,
    LeaveTypeUpdate,
)
from app.core.dates import business_today, days_inclusive, utcnow
from app.core.exceptions import DuplicateResourceError, ValidationError
from app.core.pagination import Page, PageParams
from app.domain.employees.exceptions import EmployeeNotFoundError
from app.domain.leave.enums import LeaveRequestStatus, LeaveTransactionType
from app.domain.leave.exceptions import (
    LeavePolicyNotFoundError,
    LeaveRequestNotFoundError,
    LeaveTypeNotFoundError,
)
from app.domain.leave.rules import (
    ZERO_DAYS,
    count_leave_days,
    ensure_can_cancel,
    ensure_can_decide,
    ensure_leave_dates,
    ensure_no_overlap,
    ensure_not_self_approval,
    ensure_sufficient_balance,
    ensure_within_annual_limit,
    quantize_days,
)
from app.domain.shared.enums import AuditAction
from app.domain.shared.events import (
    LeaveApproved,
    LeaveCancelled,
    LeaveRejected,
    LeaveRequested,
)
from app.infrastructure.database.models.employee import Employee
from app.infrastructure.database.models.leave import (
    EmployeeLeavePolicy,
    LeavePolicy,
    LeavePolicyRule,
    LeaveRequest,
    LeaveTransaction,
    LeaveType,
)
from app.infrastructure.database.unit_of_work import UnitOfWork
from app.infrastructure.observability import metrics

SOURCE_LEAVE_REQUEST = "LEAVE_REQUEST"
SOURCE_POLICY_GRANT = "POLICY_GRANT"
SOURCE_MANUAL = "MANUAL_ADJUSTMENT"


class LeaveService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    # ---------------------------------------------------------- leave types
    async def create_type(
        self, company_id: UUID, payload: LeaveTypeCreate, actor: ActorContext
    ) -> LeaveTypeRead:
        if await self.uow.leave_types.get_by_code(company_id, payload.leave_code):
            raise DuplicateResourceError(
                "That leave code already exists for this company.",
                details={"leave_code": payload.leave_code},
            )
        leave_type = LeaveType(
            company_id=company_id,
            **payload.model_dump(),
            is_active=True,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.leave_types.add(leave_type)
        await self.uow.flush()
        self._audit(
            AuditAction.LEAVE_TYPE_CREATED,
            "LeaveType",
            leave_type.id,
            company_id,
            actor,
            new_data={"leave_code": leave_type.leave_code, "is_paid": leave_type.is_paid},
        )
        await self.uow.commit()
        return LeaveTypeRead.model_validate(leave_type)

    async def update_type(
        self, company_id: UUID, leave_type_id: UUID, payload: LeaveTypeUpdate, actor: ActorContext
    ) -> LeaveTypeRead:
        leave_type = await self._require_type(company_id, leave_type_id)
        changes = payload.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(leave_type, field, value)
        leave_type.updated_by = actor.user_id
        await self.uow.commit()
        return LeaveTypeRead.model_validate(leave_type)

    async def list_types(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> Page[LeaveTypeRead]:
        rows, total = await self.uow.leave_types.list_for_company(
            company_id, params, is_active=is_active
        )
        return Page.build([LeaveTypeRead.model_validate(r) for r in rows], params, total=total)

    # -------------------------------------------------------------- policies
    async def create_policy(
        self, company_id: UUID, payload: LeavePolicyCreate, actor: ActorContext
    ) -> LeavePolicyRead:
        policy = LeavePolicy(
            company_id=company_id,
            name=payload.name,
            description=payload.description,
            is_active=True,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.leave_policies.add(policy)
        await self.uow.flush()

        for rule in payload.rules:
            await self._require_type(company_id, rule.leave_type_id)
            if rule.carry_forward_limit is not None and not rule.carry_forward_allowed:
                raise ValidationError(
                    "A carry-forward limit is meaningless when carry-forward is disabled.",
                    details={"leave_type_id": str(rule.leave_type_id)},
                )
            self.uow.session.add(
                LeavePolicyRule(
                    policy_id=policy.id,
                    leave_type_id=rule.leave_type_id,
                    annual_entitlement=rule.annual_entitlement,
                    carry_forward_allowed=rule.carry_forward_allowed,
                    carry_forward_limit=rule.carry_forward_limit,
                    minimum_service_days=rule.minimum_service_days,
                )
            )
        await self.uow.flush()

        self._audit(
            AuditAction.LEAVE_POLICY_CREATED,
            "LeavePolicy",
            policy.id,
            company_id,
            actor,
            new_data={"name": policy.name, "rule_count": len(payload.rules)},
        )
        await self.uow.commit()
        loaded = await self.uow.leave_policies.get_scoped(company_id, policy.id)
        assert loaded is not None
        return LeavePolicyRead.model_validate(loaded)

    async def list_policies(self, company_id: UUID, params: PageParams) -> Page[LeavePolicyRead]:
        rows, total = await self.uow.leave_policies.list_for_company(company_id, params)
        return Page.build([LeavePolicyRead.model_validate(r) for r in rows], params, total=total)

    async def assign_policy(
        self, company_id: UUID, payload: AssignPolicyRequest, actor: ActorContext
    ) -> EmployeeLeavePolicyRead:
        """Attach a policy to an employee and optionally grant this year's days.

        Overlapping assignments are refused by a GiST exclusion constraint —
        "which policy applies?" needs exactly one answer.
        """
        employee = await self._require_employee(company_id, payload.employee_id)
        policy = await self.uow.leave_policies.get_scoped(company_id, payload.leave_policy_id)
        if policy is None:
            raise LeavePolicyNotFoundError(
                details={"leave_policy_id": str(payload.leave_policy_id)}
            )

        assignment = EmployeeLeavePolicy(
            company_id=company_id,
            employee_id=employee.id,
            leave_policy_id=policy.id,
            effective_from=payload.effective_from,
            effective_to=payload.effective_to,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.session.add(assignment)
        await self.uow.flush()

        if payload.grant_entitlement:
            year = payload.effective_from.year
            for rule in policy.rules:
                service_days = days_inclusive(employee.hire_date, payload.effective_from)
                if service_days < rule.minimum_service_days:
                    continue
                await self._post_transaction(
                    company_id=company_id,
                    employee_id=employee.id,
                    leave_type_id=rule.leave_type_id,
                    year=year,
                    transaction_type=LeaveTransactionType.ENTITLEMENT,
                    days=Decimal(str(rule.annual_entitlement)),
                    source_type=SOURCE_POLICY_GRANT,
                    source_id=policy.id,
                    notes=f"Annual entitlement from policy {policy.name}",
                    actor=actor,
                )

        self._audit(
            AuditAction.LEAVE_POLICY_ASSIGNED,
            "EmployeeLeavePolicy",
            assignment.id,
            company_id,
            actor,
            new_data={
                "employee_id": str(employee.id),
                "leave_policy_id": str(policy.id),
                "effective_from": payload.effective_from.isoformat(),
                "entitlement_granted": payload.grant_entitlement,
            },
        )
        await self.uow.commit()
        return EmployeeLeavePolicyRead.model_validate(assignment)

    # -------------------------------------------------------------- requests
    async def create_request(
        self, company_id: UUID, payload: LeaveRequestCreate, employee_id: UUID, actor: ActorContext
    ) -> LeaveRequestRead:
        employee = await self._require_employee(company_id, employee_id)
        leave_type = await self._require_type(company_id, payload.leave_type_id)
        ensure_leave_dates(payload.start_date, payload.end_date)

        if payload.start_date < employee.hire_date:
            raise ValidationError(
                "Leave cannot start before the employee was hired.",
                details={
                    "start_date": payload.start_date.isoformat(),
                    "hire_date": employee.hire_date.isoformat(),
                },
            )

        existing = await self.uow.leave_requests.overlapping_ranges(employee.id)
        ensure_no_overlap(payload.start_date, payload.end_date, existing)

        holidays = await self.uow.holidays.dates_between(
            company_id,
            payload.start_date,
            payload.end_date,
            location_id=employee.location_id,
        )
        total_days = count_leave_days(
            payload.start_date,
            payload.end_date,
            holidays=holidays,
            exclude_non_working=leave_type.excludes_non_working_days,
            half_days=payload.half_days,
        )
        if total_days <= 0:
            raise ValidationError(
                "That range contains no working days for this leave type.",
                details={
                    "start_date": payload.start_date.isoformat(),
                    "end_date": payload.end_date.isoformat(),
                },
            )

        year = payload.start_date.year
        await self._check_availability(company_id, employee, leave_type, total_days, year)

        status = LeaveRequestStatus.PENDING if payload.submit else LeaveRequestStatus.DRAFT
        request = LeaveRequest(
            company_id=company_id,
            employee_id=employee.id,
            leave_type_id=leave_type.id,
            start_date=payload.start_date,
            end_date=payload.end_date,
            total_days=total_days,
            reason=payload.reason,
            status=status,
            requested_at=utcnow() if payload.submit else None,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.leave_requests.add(request)
        await self.uow.flush()

        # A leave type that needs no approval is approved on submission —
        # otherwise it would sit PENDING forever with nobody to action it.
        if payload.submit and not leave_type.requires_approval:
            await self._apply_approval(request, leave_type, actor, auto=True)

        self._audit(
            AuditAction.LEAVE_REQUESTED,
            "LeaveRequest",
            request.id,
            company_id,
            actor,
            new_data={
                "employee_id": str(employee.id),
                "leave_code": leave_type.leave_code,
                "start_date": request.start_date.isoformat(),
                "end_date": request.end_date.isoformat(),
                "total_days": str(request.total_days),
                "status": str(request.status),
            },
        )
        if payload.submit:
            self.uow.outbox.enqueue(
                LeaveRequested(
                    aggregate_id=request.id,
                    company_id=company_id,
                    employee_id=employee.id,
                    leave_type_id=leave_type.id,
                    start_date=request.start_date,
                    end_date=request.end_date,
                    total_days=total_days,
                    requested_by=actor.user_id,
                )
            )
        await self.uow.commit()
        await self.uow.refresh(request)
        return self._request_read(request, leave_type)

    async def submit_request(
        self, company_id: UUID, request_id: UUID, actor: ActorContext
    ) -> LeaveRequestRead:
        request = await self._require_request_locked(company_id, request_id)
        from app.domain.leave.rules import ensure_can_submit

        ensure_can_submit(request.status)
        leave_type = await self._require_type(company_id, request.leave_type_id)

        request.status = LeaveRequestStatus.PENDING
        request.requested_at = utcnow()
        request.version += 1

        if not leave_type.requires_approval:
            await self._apply_approval(request, leave_type, actor, auto=True)

        await self.uow.commit()
        return self._request_read(request, leave_type)

    async def approve(
        self, company_id: UUID, request_id: UUID, actor: ActorContext
    ) -> LeaveRequestRead:
        request = await self._require_request_locked(company_id, request_id)
        ensure_can_decide(request.status)
        # Separation of duties, enforced regardless of role: an HR Manager who
        # is also an employee still cannot sign off their own holiday.
        ensure_not_self_approval(request.employee_id, actor.employee_id)

        leave_type = await self._require_type(company_id, request.leave_type_id)
        employee = await self._require_employee(company_id, request.employee_id)

        # Re-check the balance at approval time. It was checked at request
        # time too, but days may have been consumed in between.
        await self._check_availability(
            company_id,
            employee,
            leave_type,
            Decimal(str(request.total_days)),
            request.start_date.year,
            exclude_request_id=request.id,
        )

        await self._apply_approval(request, leave_type, actor, auto=False)
        await self.uow.commit()

        metrics.leave_decisions_total.labels(str(company_id), "approved").inc()
        return self._request_read(request, leave_type)

    async def reject(
        self, company_id: UUID, request_id: UUID, reason: str, actor: ActorContext
    ) -> LeaveRequestRead:
        request = await self._require_request_locked(company_id, request_id)
        ensure_can_decide(request.status)
        ensure_not_self_approval(request.employee_id, actor.employee_id)

        leave_type = await self._require_type(company_id, request.leave_type_id)
        previous = {"status": str(request.status)}

        request.status = LeaveRequestStatus.REJECTED
        request.rejected_at = utcnow()
        request.rejected_by = actor.user_id
        request.rejection_reason = reason
        request.version += 1

        # No balance transaction: a pending request never consumed any.
        self._audit(
            AuditAction.LEAVE_REJECTED,
            "LeaveRequest",
            request.id,
            company_id,
            actor,
            previous_data=previous,
            new_data={"status": str(request.status), "rejection_reason": reason},
        )
        self.uow.outbox.enqueue(
            LeaveRejected(
                aggregate_id=request.id,
                company_id=company_id,
                employee_id=request.employee_id,
                rejection_reason=reason,
                rejected_by=actor.user_id,
            )
        )
        await self.uow.commit()

        metrics.leave_decisions_total.labels(str(company_id), "rejected").inc()
        return self._request_read(request, leave_type)

    async def cancel(
        self, company_id: UUID, request_id: UUID, actor: ActorContext
    ) -> LeaveRequestRead:
        request = await self._require_request_locked(company_id, request_id)
        leave_type = await self._require_type(company_id, request.leave_type_id)
        employee = await self._require_employee(company_id, request.employee_id)
        today = business_today(await self._timezone_for(employee))
        ensure_can_cancel(request.status, request.start_date, today=today)

        was_approved = request.status is LeaveRequestStatus.APPROVED
        previous = {"status": str(request.status)}

        request.status = LeaveRequestStatus.CANCELLED
        request.cancelled_at = utcnow()
        request.cancelled_by = actor.user_id
        request.version += 1

        if was_approved:
            # Approved leave *did* consume balance, so cancelling gives it
            # back — by reducing `used`, not by inflating `earned`. The
            # employee never earned those days twice.
            await self._post_transaction(
                company_id=company_id,
                employee_id=request.employee_id,
                leave_type_id=request.leave_type_id,
                year=request.start_date.year,
                transaction_type=LeaveTransactionType.CANCELLATION,
                days=Decimal(str(request.total_days)),
                source_type=SOURCE_LEAVE_REQUEST,
                source_id=request.id,
                notes="Approved leave cancelled",
                actor=actor,
            )

        self._audit(
            AuditAction.LEAVE_CANCELLED,
            "LeaveRequest",
            request.id,
            company_id,
            actor,
            previous_data=previous,
            new_data={"status": str(request.status), "balance_returned": was_approved},
        )
        self.uow.outbox.enqueue(
            LeaveCancelled(
                aggregate_id=request.id,
                company_id=company_id,
                employee_id=request.employee_id,
                total_days=Decimal(str(request.total_days)),
                was_approved=was_approved,
                cancelled_by=actor.user_id,
            )
        )
        await self.uow.commit()

        metrics.leave_decisions_total.labels(str(company_id), "cancelled").inc()
        return self._request_read(request, leave_type)

    async def get_request(self, company_id: UUID, request_id: UUID) -> LeaveRequestRead:
        request = await self._require_request(company_id, request_id)
        return self._request_read(request, request.leave_type)

    async def list_requests(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        employee_id: UUID | None = None,
        leave_type_id: UUID | None = None,
        status: LeaveRequestStatus | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> Page[LeaveRequestRead]:
        rows, total = await self.uow.leave_requests.list_paginated(
            company_id,
            params,
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            status=status,
            start_date=start_date,
            end_date=end_date,
        )
        return Page.build([self._request_read(r, r.leave_type) for r in rows], params, total=total)

    # -------------------------------------------------------------- balances
    async def balances_for_employee(
        self, company_id: UUID, employee_id: UUID, year: int
    ) -> list[LeaveBalanceRead]:
        await self._require_employee(company_id, employee_id)
        rows = await self.uow.leave_balances.list_for_employee(company_id, employee_id, year)

        results: list[LeaveBalanceRead] = []
        for row in rows:
            leave_type = await self.uow.leave_types.get_scoped(company_id, row.leave_type_id)
            pending = await self.uow.leave_requests.pending_days(
                employee_id, row.leave_type_id, year
            )
            closing = Decimal(str(row.closing_balance))
            results.append(
                LeaveBalanceRead(
                    company_id=row.company_id,
                    employee_id=row.employee_id,
                    leave_type_id=row.leave_type_id,
                    leave_code=leave_type.leave_code if leave_type else None,
                    leave_name=leave_type.name if leave_type else None,
                    year=row.year,
                    opening_balance=Decimal(str(row.opening_balance)),
                    earned=Decimal(str(row.earned)),
                    used=Decimal(str(row.used)),
                    adjustment=Decimal(str(row.adjustment)),
                    closing_balance=closing,
                    pending_days=pending,
                    available=quantize_days(closing - pending),
                )
            )
        return sorted(results, key=lambda b: b.leave_code or "")

    async def adjust_balance(
        self, company_id: UUID, payload: BalanceAdjustmentRequest, actor: ActorContext
    ) -> LeaveBalanceRead:
        """Manually correct a balance. Always requires a written reason.

        This is the sanctioned way to fix a mistake — including returning days
        for leave that had already started, which `cancel` deliberately
        refuses to do silently.
        """
        await self._require_employee(company_id, payload.employee_id)
        leave_type = await self._require_type(company_id, payload.leave_type_id)

        await self._post_transaction(
            company_id=company_id,
            employee_id=payload.employee_id,
            leave_type_id=payload.leave_type_id,
            year=payload.year,
            transaction_type=LeaveTransactionType.ADJUSTMENT,
            days=payload.days,
            source_type=SOURCE_MANUAL,
            source_id=None,
            notes=payload.notes,
            actor=actor,
        )
        self._audit(
            AuditAction.LEAVE_BALANCE_ADJUSTED,
            "LeaveBalance",
            payload.employee_id,
            company_id,
            actor,
            new_data={
                "employee_id": str(payload.employee_id),
                "leave_code": leave_type.leave_code,
                "year": payload.year,
                "days": str(payload.days),
                "notes": payload.notes,
            },
        )
        await self.uow.commit()

        balances = await self.balances_for_employee(company_id, payload.employee_id, payload.year)
        for balance in balances:
            if balance.leave_type_id == payload.leave_type_id:
                return balance
        raise LeaveTypeNotFoundError(details={"leave_type_id": str(payload.leave_type_id)})

    async def list_transactions(
        self, company_id: UUID, employee_id: UUID, leave_type_id: UUID, year: int
    ) -> list[LeaveTransactionRead]:
        await self._require_employee(company_id, employee_id)
        rows = await self.uow.leave_transactions.list_for_balance(
            company_id, employee_id, leave_type_id, year
        )
        return [LeaveTransactionRead.model_validate(r) for r in rows]

    # ------------------------------------------------------------- internals
    async def _apply_approval(
        self,
        request: LeaveRequest,
        leave_type: LeaveType,
        actor: ActorContext,
        *,
        auto: bool,
    ) -> None:
        previous = {"status": str(request.status)}
        request.status = LeaveRequestStatus.APPROVED
        request.approved_at = utcnow()
        request.approved_by = actor.user_id
        request.version += 1

        await self._post_transaction(
            company_id=request.company_id,
            employee_id=request.employee_id,
            leave_type_id=request.leave_type_id,
            year=request.start_date.year,
            transaction_type=LeaveTransactionType.CONSUMPTION,
            days=Decimal(str(request.total_days)),
            source_type=SOURCE_LEAVE_REQUEST,
            source_id=request.id,
            notes="Auto-approved (no approval required)" if auto else "Leave approved",
            actor=actor,
        )

        self._audit(
            AuditAction.LEAVE_APPROVED,
            "LeaveRequest",
            request.id,
            request.company_id,
            actor,
            previous_data=previous,
            new_data={
                "status": str(request.status),
                "total_days": str(request.total_days),
                "auto_approved": auto,
            },
        )
        self.uow.outbox.enqueue(
            LeaveApproved(
                aggregate_id=request.id,
                company_id=request.company_id,
                employee_id=request.employee_id,
                leave_type_id=request.leave_type_id,
                start_date=request.start_date,
                end_date=request.end_date,
                total_days=Decimal(str(request.total_days)),
                is_paid=leave_type.is_paid,
                approved_by=actor.user_id,
            )
        )

    async def _post_transaction(
        self,
        *,
        company_id: UUID,
        employee_id: UUID,
        leave_type_id: UUID,
        year: int,
        transaction_type: LeaveTransactionType,
        days: Decimal,
        source_type: str | None,
        source_id: UUID | None,
        notes: str | None,
        actor: ActorContext,
    ) -> None:
        """Append a movement and fold it into the projection atomically.

        The transaction row is the truth; the balance row is a projection of
        it. An ADJUSTMENT may be negative, so it is stored as a signed value
        in the adjustment column — every other type stores a magnitude and the
        column it lands in carries the sign.
        """
        magnitude = abs(days) if transaction_type is not LeaveTransactionType.ADJUSTMENT else days

        self.uow.leave_transactions.add(
            LeaveTransaction(
                company_id=company_id,
                employee_id=employee_id,
                leave_type_id=leave_type_id,
                year=year,
                transaction_type=transaction_type,
                days=abs(magnitude),
                source_type=source_type,
                source_id=source_id,
                notes=(
                    f"{notes} ({magnitude:+} days)"
                    if transaction_type is LeaveTransactionType.ADJUSTMENT
                    else notes
                ),
                created_by=actor.user_id,
            )
        )

        deltas = {
            "opening_delta": ZERO_DAYS,
            "earned_delta": ZERO_DAYS,
            "used_delta": ZERO_DAYS,
            "adjustment_delta": ZERO_DAYS,
        }
        match transaction_type:
            case LeaveTransactionType.CARRY_FORWARD:
                deltas["opening_delta"] = abs(magnitude)
            case LeaveTransactionType.ENTITLEMENT | LeaveTransactionType.ACCRUAL:
                deltas["earned_delta"] = abs(magnitude)
            case LeaveTransactionType.CONSUMPTION:
                deltas["used_delta"] = abs(magnitude)
            case LeaveTransactionType.CANCELLATION:
                deltas["used_delta"] = -abs(magnitude)
            case LeaveTransactionType.ADJUSTMENT | LeaveTransactionType.EXPIRY:
                deltas["adjustment_delta"] = magnitude

        await self.uow.leave_balances.apply_movement(
            company_id=company_id,
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            year=year,
            **deltas,
        )

    async def _check_availability(
        self,
        company_id: UUID,
        employee: Employee,
        leave_type: LeaveType,
        requested_days: Decimal,
        year: int,
        *,
        exclude_request_id: UUID | None = None,
    ) -> None:
        snapshot = await self.uow.leave_balances.snapshot(
            company_id, employee.id, leave_type.id, year
        )
        pending = await self.uow.leave_requests.pending_days(employee.id, leave_type.id, year)
        if exclude_request_id is not None:
            # The request being approved is itself pending; do not make it
            # compete with its own reservation.
            pending = max(ZERO_DAYS, pending - requested_days)

        ensure_sufficient_balance(
            requested_days=requested_days,
            snapshot=snapshot,
            pending_days=pending,
            leave_code=leave_type.leave_code,
            allow_negative=leave_type.allows_negative_balance,
        )
        used = await self.uow.leave_requests.used_days_in_year(employee.id, leave_type.id, year)
        ensure_within_annual_limit(
            requested_days=requested_days,
            used_this_year=used,
            annual_limit=(
                Decimal(str(leave_type.annual_limit))
                if leave_type.annual_limit is not None
                else None
            ),
            leave_code=leave_type.leave_code,
        )

    async def _timezone_for(self, employee: Employee) -> str | None:
        company = await self.uow.companies.get(employee.company_id)
        return company.timezone if company else None

    async def _require_employee(self, company_id: UUID, employee_id: UUID) -> Employee:
        row = await self.uow.employees.get_scoped(company_id, employee_id)
        if row is None:
            raise EmployeeNotFoundError(details={"employee_id": str(employee_id)})
        return row

    async def _require_type(self, company_id: UUID, leave_type_id: UUID) -> LeaveType:
        row = await self.uow.leave_types.get_scoped(company_id, leave_type_id)
        if row is None:
            raise LeaveTypeNotFoundError(details={"leave_type_id": str(leave_type_id)})
        return row

    async def _require_request(self, company_id: UUID, request_id: UUID) -> LeaveRequest:
        row = await self.uow.leave_requests.get_scoped(company_id, request_id)
        if row is None:
            raise LeaveRequestNotFoundError(details={"request_id": str(request_id)})
        return row

    async def _require_request_locked(self, company_id: UUID, request_id: UUID) -> LeaveRequest:
        row = await self.uow.leave_requests.get_for_update(company_id, request_id)
        if row is None:
            raise LeaveRequestNotFoundError(details={"request_id": str(request_id)})
        return row

    @staticmethod
    def _request_read(request: LeaveRequest, leave_type: LeaveType | None) -> LeaveRequestRead:
        read = LeaveRequestRead.model_validate(request)
        if leave_type is not None:
            return read.model_copy(update={"leave_code": leave_type.leave_code})
        return read

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
