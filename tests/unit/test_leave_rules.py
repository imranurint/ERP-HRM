"""Leave day counting, the balance identity and the request state machine."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.leave.enums import LeaveRequestStatus, LeaveTransactionType
from app.domain.leave.exceptions import (
    InvalidLeaveDatesError,
    LeaveBalanceInsufficientError,
    LeaveExceedsAnnualLimitError,
    LeaveNotCancellableError,
    LeaveNotPendingError,
    LeaveOverlapError,
    SelfApprovalError,
)
from app.domain.leave.rules import (
    ZERO_DAYS,
    LeaveBalanceSnapshot,
    apply_transaction,
    carry_forward_days,
    count_leave_days,
    ensure_can_cancel,
    ensure_can_decide,
    ensure_leave_dates,
    ensure_no_overlap,
    ensure_not_self_approval,
    ensure_sufficient_balance,
    ensure_within_annual_limit,
    replay,
)

pytestmark = pytest.mark.unit


def snapshot(
    opening: str = "0", earned: str = "25", used: str = "0", adjustment: str = "0"
) -> LeaveBalanceSnapshot:
    return LeaveBalanceSnapshot(
        opening_balance=Decimal(opening),
        earned=Decimal(earned),
        used=Decimal(used),
        adjustment=Decimal(adjustment),
    )


class TestDayCounting:
    def test_weekends_are_excluded_by_default(self) -> None:
        """Friday to Monday over a normal weekend is two days, not four."""
        # 2026-03-06 is a Friday; 2026-03-09 is the Monday.
        assert count_leave_days(date(2026, 3, 6), date(2026, 3, 9)) == Decimal("2.00")

    def test_holidays_are_excluded(self) -> None:
        days = count_leave_days(date(2026, 3, 16), date(2026, 3, 18), holidays=[date(2026, 3, 17)])
        assert days == Decimal("2.00")

    def test_calendar_day_leave_types_count_everything(self) -> None:
        days = count_leave_days(date(2026, 3, 6), date(2026, 3, 9), exclude_non_working=False)
        assert days == Decimal("4.00")

    def test_half_days_count_as_a_half(self) -> None:
        days = count_leave_days(date(2026, 3, 2), date(2026, 3, 3), half_days=[date(2026, 3, 3)])
        assert days == Decimal("1.50")

    def test_halves_add_up_exactly(self) -> None:
        """Decimal, not float: 0.5 + 0.5 + 0.5 must be exactly 1.5."""
        days = count_leave_days(
            date(2026, 3, 2),
            date(2026, 3, 4),
            half_days=[date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)],
        )
        assert days == Decimal("1.50")

    def test_end_before_start_is_rejected(self) -> None:
        with pytest.raises(InvalidLeaveDatesError):
            ensure_leave_dates(date(2026, 3, 10), date(2026, 3, 1))

    def test_an_absurdly_long_span_is_rejected(self) -> None:
        with pytest.raises(InvalidLeaveDatesError):
            ensure_leave_dates(date(2026, 1, 1), date(2030, 1, 1))


class TestBalanceIdentity:
    def test_closing_is_derived_from_its_parts(self) -> None:
        assert snapshot(earned="25", used="5").closing_balance == Decimal("20.00")

    def test_adjustment_can_be_negative(self) -> None:
        assert snapshot(earned="25", used="5", adjustment="-2").closing_balance == Decimal("18.00")

    def test_available_subtracts_pending(self) -> None:
        """Two requests the same morning cannot both spend one remaining day."""
        assert snapshot(earned="25", used="20").available(Decimal("3")) == Decimal("2.00")

    def test_replay_reproduces_a_balance(self) -> None:
        """The property the rebuild script depends on."""
        result = replay(
            [
                (LeaveTransactionType.ENTITLEMENT, Decimal("25")),
                (LeaveTransactionType.CONSUMPTION, Decimal("5")),
                (LeaveTransactionType.CANCELLATION, Decimal("2")),
                (LeaveTransactionType.ADJUSTMENT, Decimal("1")),
            ]
        )
        assert result.earned == Decimal("25.00")
        assert result.used == Decimal("3.00")
        assert result.adjustment == Decimal("1.00")
        assert result.closing_balance == Decimal("23.00")

    def test_cancellation_reduces_used_rather_than_inflating_earned(self) -> None:
        """The employee never earned those days twice."""
        after = apply_transaction(
            snapshot(earned="25", used="5"),
            LeaveTransactionType.CANCELLATION,
            Decimal("5"),
        )
        assert after.used == Decimal("0.00")
        assert after.earned == Decimal("25.00")

    def test_carry_forward_lands_in_opening_balance(self) -> None:
        after = apply_transaction(
            snapshot(earned="0"), LeaveTransactionType.CARRY_FORWARD, Decimal("5")
        )
        assert after.opening_balance == Decimal("5.00")


class TestSufficiency:
    def test_a_request_within_balance_is_allowed(self) -> None:
        ensure_sufficient_balance(
            requested_days=Decimal("5"), snapshot=snapshot(), leave_code="ANNUAL"
        )

    def test_a_request_beyond_balance_is_refused(self) -> None:
        with pytest.raises(LeaveBalanceInsufficientError) as excinfo:
            ensure_sufficient_balance(
                requested_days=Decimal("30"), snapshot=snapshot(), leave_code="ANNUAL"
            )
        assert excinfo.value.details["shortfall_days"] == "5.00"

    def test_pending_requests_reserve_balance(self) -> None:
        with pytest.raises(LeaveBalanceInsufficientError):
            ensure_sufficient_balance(
                requested_days=Decimal("5"),
                snapshot=snapshot(earned="25", used="20"),
                pending_days=Decimal("3"),
                leave_code="ANNUAL",
            )

    def test_negative_balance_is_allowed_when_the_type_permits(self) -> None:
        ensure_sufficient_balance(
            requested_days=Decimal("100"),
            snapshot=snapshot(),
            leave_code="ADVANCE",
            allow_negative=True,
        )

    def test_annual_limit_is_separate_from_balance(self) -> None:
        with pytest.raises(LeaveExceedsAnnualLimitError):
            ensure_within_annual_limit(
                requested_days=Decimal("5"),
                used_this_year=Decimal("8"),
                annual_limit=Decimal("10"),
                leave_code="SICK",
            )

    def test_no_annual_limit_means_no_check(self) -> None:
        ensure_within_annual_limit(
            requested_days=Decimal("500"),
            used_this_year=Decimal("0"),
            annual_limit=None,
            leave_code="UNPAID",
        )


class TestStateMachine:
    def test_only_pending_can_be_decided(self) -> None:
        with pytest.raises(LeaveNotPendingError):
            ensure_can_decide(LeaveRequestStatus.APPROVED)

    def test_pending_can_be_decided(self) -> None:
        ensure_can_decide(LeaveRequestStatus.PENDING)

    def test_nobody_approves_their_own_leave(self) -> None:
        """Enforced regardless of role — a grant is not a loophole."""
        employee = "emp-1"
        with pytest.raises(SelfApprovalError):
            ensure_not_self_approval(employee, employee)

    def test_somebody_else_may_approve(self) -> None:
        ensure_not_self_approval("emp-1", "emp-2")

    def test_an_approver_with_no_employee_record_is_allowed(self) -> None:
        """A system administrator with no employee record is not self-approving."""
        ensure_not_self_approval("emp-1", None)

    def test_future_approved_leave_can_be_cancelled(self) -> None:
        ensure_can_cancel(LeaveRequestStatus.APPROVED, date(2026, 6, 1), today=date(2026, 3, 1))

    def test_started_approved_leave_cannot_be_cancelled(self) -> None:
        """Cancelling would silently return days actually taken; the correct
        remedy is an explicit, audited balance adjustment."""
        with pytest.raises(LeaveNotCancellableError):
            ensure_can_cancel(LeaveRequestStatus.APPROVED, date(2026, 3, 1), today=date(2026, 3, 5))

    def test_an_already_cancelled_request_cannot_be_cancelled_again(self) -> None:
        with pytest.raises(LeaveNotCancellableError):
            ensure_can_cancel(
                LeaveRequestStatus.CANCELLED, date(2026, 6, 1), today=date(2026, 3, 1)
            )


class TestOverlap:
    def test_overlapping_leave_is_refused(self) -> None:
        with pytest.raises(LeaveOverlapError):
            ensure_no_overlap(
                date(2026, 3, 10),
                date(2026, 3, 12),
                [(date(2026, 3, 11), date(2026, 3, 15))],
            )

    def test_adjacent_leave_is_fine(self) -> None:
        ensure_no_overlap(
            date(2026, 3, 16), date(2026, 3, 20), [(date(2026, 3, 10), date(2026, 3, 13))]
        )


class TestCarryForward:
    def test_carry_forward_is_capped(self) -> None:
        assert carry_forward_days(
            closing_balance=Decimal("12"),
            carry_forward_allowed=True,
            carry_forward_limit=Decimal("5"),
        ) == Decimal("5.00")

    def test_uncapped_carry_forward_takes_the_whole_balance(self) -> None:
        assert carry_forward_days(
            closing_balance=Decimal("12"),
            carry_forward_allowed=True,
            carry_forward_limit=None,
        ) == Decimal("12.00")

    def test_disallowed_carry_forward_yields_nothing(self) -> None:
        assert (
            carry_forward_days(
                closing_balance=Decimal("12"),
                carry_forward_allowed=False,
                carry_forward_limit=None,
            )
            == ZERO_DAYS
        )

    def test_a_negative_balance_never_carries_forward(self) -> None:
        assert (
            carry_forward_days(
                closing_balance=Decimal("-3"),
                carry_forward_allowed=True,
                carry_forward_limit=None,
            )
            == ZERO_DAYS
        )
