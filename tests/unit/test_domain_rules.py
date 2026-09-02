"""Employee lifecycle, attendance and payroll-immutability rules.

Pure functions, no database. These are the guards every mutation calls.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.dates import days_inclusive, is_weekend, minutes_between, spans_midnight
from app.domain.attendance.enums import AttendanceStatus
from app.domain.attendance.exceptions import InvalidAttendanceWindowError, InvalidShiftError
from app.domain.attendance.rules import (
    ShiftDefinition,
    compute,
    ensure_shift_valid,
    late_minutes,
    overtime_minutes,
    payable_day_fraction,
)
from app.domain.employees.enums import ContractStatus, ContractType, EmploymentStatus
from app.domain.employees.exceptions import (
    ContractImmutableError,
    InvalidContractError,
    InvalidEmploymentStatusError,
    InvalidHierarchyError,
    InvalidManagerError,
)
from app.domain.employees.rules import (
    contract_status_on,
    ensure_contract_dates,
    ensure_contract_mutable,
    ensure_employed,
    ensure_no_department_cycle,
    ensure_no_manager_cycle,
    ensure_status_transition,
    ensure_termination_date,
)
from app.domain.payroll.enums import PayrollRunStatus, PayslipStatus
from app.domain.payroll.exceptions import (
    PayrollAlreadyApprovedError,
    PayrollAlreadyPaidError,
    PayrollNotApprovedError,
    PayrollNotCalculatedError,
    PayrollPeriodOverlapError,
    PayslipCannotBeModifiedError,
    SalaryStructureOverlapError,
)
from app.domain.payroll.rules import (
    ensure_can_approve,
    ensure_can_pay,
    ensure_no_period_overlap,
    ensure_no_structure_overlap,
    ensure_payslip_mutable,
    ensure_payslip_voidable,
    ensure_run_mutable,
)

pytestmark = pytest.mark.unit


class TestEmploymentStatusTransitions:
    def test_probation_confirms_to_active(self) -> None:
        ensure_status_transition(EmploymentStatus.PROBATION, EmploymentStatus.ACTIVE)

    def test_terminated_is_terminal(self) -> None:
        """A returning employee is a rehire, not a resurrection — that keeps
        tenure and leave accrual honest."""
        with pytest.raises(InvalidEmploymentStatusError):
            ensure_status_transition(EmploymentStatus.TERMINATED, EmploymentStatus.ACTIVE)

    def test_resigned_is_terminal(self) -> None:
        with pytest.raises(InvalidEmploymentStatusError):
            ensure_status_transition(EmploymentStatus.RESIGNED, EmploymentStatus.ACTIVE)

    def test_a_transition_to_the_current_status_is_rejected(self) -> None:
        with pytest.raises(InvalidEmploymentStatusError):
            ensure_status_transition(EmploymentStatus.ACTIVE, EmploymentStatus.ACTIVE)

    def test_suspended_can_be_reinstated(self) -> None:
        ensure_status_transition(EmploymentStatus.SUSPENDED, EmploymentStatus.ACTIVE)

    def test_ensure_employed_blocks_action_on_a_leaver(self) -> None:
        with pytest.raises(InvalidEmploymentStatusError):
            ensure_employed(EmploymentStatus.TERMINATED, action="promote this employee")

    @pytest.mark.parametrize(
        ("status", "employed"),
        [
            (EmploymentStatus.ACTIVE, True),
            (EmploymentStatus.PROBATION, True),
            (EmploymentStatus.ON_LEAVE, True),
            (EmploymentStatus.SUSPENDED, False),
            (EmploymentStatus.TERMINATED, False),
            (EmploymentStatus.INACTIVE, False),
        ],
    )
    def test_is_employed_classification(self, status: EmploymentStatus, employed: bool) -> None:
        assert status.is_employed is employed

    def test_suspended_is_still_payable(self) -> None:
        """Whether suspension is paid is a salary-structure question, not one
        the payroll engine should answer by excluding the person."""
        assert EmploymentStatus.SUSPENDED.is_payable is True
        assert EmploymentStatus.INACTIVE.is_payable is False


class TestHierarchies:
    def test_self_management_is_rejected(self) -> None:
        employee = uuid4()
        with pytest.raises(InvalidManagerError):
            ensure_no_manager_cycle(employee, employee, [])

    def test_reporting_cycle_is_rejected(self) -> None:
        employee, manager = uuid4(), uuid4()
        # The proposed manager already reports to this employee.
        with pytest.raises(InvalidHierarchyError):
            ensure_no_manager_cycle(employee, manager, [manager, employee])

    def test_a_valid_chain_is_accepted(self) -> None:
        employee, manager, director = uuid4(), uuid4(), uuid4()
        ensure_no_manager_cycle(employee, manager, [manager, director])

    def test_department_cannot_be_its_own_parent(self) -> None:
        department = uuid4()
        with pytest.raises(InvalidHierarchyError):
            ensure_no_department_cycle(department, department, [])

    def test_department_cannot_be_parented_to_its_descendant(self) -> None:
        parent, child = uuid4(), uuid4()
        with pytest.raises(InvalidHierarchyError):
            ensure_no_department_cycle(parent, child, [child, parent])


class TestContracts:
    def test_fixed_term_requires_an_end_date(self) -> None:
        """Without one the contract can never expire and payroll would keep
        paying against it forever."""
        with pytest.raises(InvalidContractError):
            ensure_contract_dates(
                contract_type=ContractType.CONTRACT,
                start_date=date(2026, 1, 1),
                end_date=None,
                probation_end_date=None,
            )

    def test_permanent_may_be_open_ended(self) -> None:
        ensure_contract_dates(
            contract_type=ContractType.PERMANENT,
            start_date=date(2026, 1, 1),
            end_date=None,
            probation_end_date=None,
        )

    def test_end_before_start_is_rejected(self) -> None:
        with pytest.raises(InvalidContractError):
            ensure_contract_dates(
                contract_type=ContractType.PERMANENT,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 1, 1),
                probation_end_date=None,
            )

    def test_probation_cannot_outlast_the_contract(self) -> None:
        with pytest.raises(InvalidContractError):
            ensure_contract_dates(
                contract_type=ContractType.CONTRACT,
                start_date=date(2026, 1, 1),
                end_date=date(2026, 6, 30),
                probation_end_date=date(2026, 12, 31),
            )

    def test_expiry_is_derived_not_scheduled(self) -> None:
        """A missed cron job must not let payroll pay a lapsed contract."""
        assert (
            contract_status_on(
                status=ContractStatus.ACTIVE,
                end_date=date(2026, 1, 31),
                as_of=date(2026, 3, 1),
            )
            is ContractStatus.EXPIRED
        )

    def test_still_active_before_its_end_date(self) -> None:
        assert (
            contract_status_on(
                status=ContractStatus.ACTIVE,
                end_date=date(2026, 12, 31),
                as_of=date(2026, 3, 1),
            )
            is ContractStatus.ACTIVE
        )

    def test_final_contracts_are_immutable(self) -> None:
        with pytest.raises(ContractImmutableError):
            ensure_contract_mutable(ContractStatus.TERMINATED)

    def test_termination_before_hire_is_rejected(self) -> None:
        with pytest.raises(InvalidContractError):
            ensure_termination_date(date(2026, 6, 1), date(2026, 1, 1))


class TestAttendanceArithmetic:
    def test_overnight_shift_spans_midnight(self) -> None:
        assert spans_midnight(time(22, 0), time(6, 0)) is True
        assert minutes_between(time(22, 0), time(6, 0)) == 480

    def test_day_shift_does_not(self) -> None:
        assert spans_midnight(time(9, 0), time(17, 0)) is False
        assert minutes_between(time(9, 0), time(17, 0)) == 480

    def test_grace_makes_a_small_delay_exactly_zero_late(self) -> None:
        """Grace exists so a two-minute delay never becomes a deduction."""
        start = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        assert late_minutes(start, datetime(2026, 3, 2, 9, 8, tzinfo=UTC), 10) == 0

    def test_beyond_grace_counts_the_whole_delay(self) -> None:
        start = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        assert late_minutes(start, datetime(2026, 3, 2, 9, 20, tzinfo=UTC), 10) == 20

    def test_overtime_only_past_the_shift(self) -> None:
        assert overtime_minutes(480, 480) == 0
        assert overtime_minutes(540, 480) == 60

    def test_shift_validation_rejects_impossible_hours(self) -> None:
        with pytest.raises(InvalidShiftError):
            ensure_shift_valid(time(9, 0), time(17, 0), Decimal("12"))

    def test_check_out_before_check_in_is_rejected(self) -> None:
        shift = ShiftDefinition(time(9, 0), time(17, 0), 10, 480)
        with pytest.raises(InvalidAttendanceWindowError):
            compute(
                shift=shift,
                scheduled_start=datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
                scheduled_end=datetime(2026, 3, 2, 17, 0, tzinfo=UTC),
                check_in=datetime(2026, 3, 2, 17, 0, tzinfo=UTC),
                check_out=datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
            )

    def test_a_normal_day_is_present(self) -> None:
        shift = ShiftDefinition(time(9, 0), time(17, 0), 10, 480)
        result = compute(
            shift=shift,
            scheduled_start=datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
            scheduled_end=datetime(2026, 3, 2, 17, 0, tzinfo=UTC),
            check_in=datetime(2026, 3, 2, 9, 5, tzinfo=UTC),
            check_out=datetime(2026, 3, 2, 17, 30, tzinfo=UTC),
        )
        assert result.status is AttendanceStatus.PRESENT
        assert result.working_minutes == 505
        assert result.late_minutes == 0
        assert result.overtime_minutes == 25

    def test_a_short_day_is_a_half_day_even_when_late(self) -> None:
        """Half-day outranks late: proration cares about the bigger problem."""
        shift = ShiftDefinition(time(9, 0), time(17, 0), 10, 480)
        result = compute(
            shift=shift,
            scheduled_start=datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
            scheduled_end=datetime(2026, 3, 2, 17, 0, tzinfo=UTC),
            check_in=datetime(2026, 3, 2, 10, 0, tzinfo=UTC),
            check_out=datetime(2026, 3, 2, 12, 0, tzinfo=UTC),
        )
        assert result.status is AttendanceStatus.HALF_DAY

    def test_no_check_in_is_absent(self) -> None:
        shift = ShiftDefinition(time(9, 0), time(17, 0), 10, 480)
        result = compute(
            shift=shift,
            scheduled_start=datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
            scheduled_end=datetime(2026, 3, 2, 17, 0, tzinfo=UTC),
            check_in=None,
            check_out=None,
        )
        assert result.status is AttendanceStatus.ABSENT

    def test_work_on_a_non_working_day_is_all_overtime(self) -> None:
        shift = ShiftDefinition(time(9, 0), time(17, 0), 10, 480)
        result = compute(
            shift=shift,
            scheduled_start=datetime(2026, 3, 7, 9, 0, tzinfo=UTC),
            scheduled_end=datetime(2026, 3, 7, 17, 0, tzinfo=UTC),
            check_in=datetime(2026, 3, 7, 10, 0, tzinfo=UTC),
            check_out=datetime(2026, 3, 7, 14, 0, tzinfo=UTC),
            non_working_day_status=AttendanceStatus.WEEKEND,
        )
        assert result.status is AttendanceStatus.WEEKEND
        assert result.overtime_minutes == 240
        assert result.late_minutes == 0

    @pytest.mark.parametrize(
        ("status", "fraction"),
        [
            (AttendanceStatus.PRESENT, Decimal("1")),
            (AttendanceStatus.LATE, Decimal("1")),
            (AttendanceStatus.HALF_DAY, Decimal("0.5")),
            (AttendanceStatus.ABSENT, Decimal("0")),
            (AttendanceStatus.HOLIDAY, Decimal("1")),
            (AttendanceStatus.WEEKEND, Decimal("1")),
        ],
    )
    def test_payable_fraction(self, status: AttendanceStatus, fraction: Decimal) -> None:
        assert payable_day_fraction(status) == fraction

    def test_weekend_detection_is_configurable(self) -> None:
        saturday = date(2026, 3, 7)
        assert is_weekend(saturday) is True
        # Some markets rest Friday/Saturday instead.
        assert is_weekend(saturday, frozenset({4, 5})) is True
        assert is_weekend(date(2026, 3, 5), frozenset({4, 5})) is False

    def test_days_inclusive_counts_both_ends(self) -> None:
        assert days_inclusive(date(2026, 3, 1), date(2026, 3, 1)) == 1
        assert days_inclusive(date(2026, 3, 1), date(2026, 3, 31)) == 31
        assert days_inclusive(date(2026, 3, 31), date(2026, 3, 1)) == 0


class TestPayrollImmutability:
    """Golden Rules 2, 3 and 7."""

    def test_approved_run_cannot_be_recalculated(self) -> None:
        with pytest.raises(PayrollAlreadyApprovedError):
            ensure_run_mutable(PayrollRunStatus.APPROVED)

    def test_paid_run_cannot_be_recalculated(self) -> None:
        with pytest.raises(PayrollAlreadyPaidError):
            ensure_run_mutable(PayrollRunStatus.PAID)

    def test_a_failed_run_may_be_re_run(self) -> None:
        """A run that blew up halfway is meant to be fixed, not abandoned."""
        ensure_run_mutable(PayrollRunStatus.FAILED)

    def test_only_a_completed_run_can_be_approved(self) -> None:
        with pytest.raises(PayrollNotCalculatedError):
            ensure_can_approve(PayrollRunStatus.DRAFT)

    def test_approving_twice_is_refused(self) -> None:
        with pytest.raises(PayrollAlreadyApprovedError):
            ensure_can_approve(PayrollRunStatus.APPROVED)

    def test_only_an_approved_run_can_be_paid(self) -> None:
        with pytest.raises(PayrollNotApprovedError):
            ensure_can_pay(PayrollRunStatus.COMPLETED)

    def test_paying_twice_is_refused(self) -> None:
        with pytest.raises(PayrollAlreadyPaidError):
            ensure_can_pay(PayrollRunStatus.PAID)

    def test_approved_payslip_is_immutable(self) -> None:
        with pytest.raises(PayslipCannotBeModifiedError):
            ensure_payslip_mutable(PayslipStatus.APPROVED)

    def test_paid_payslip_cannot_be_voided(self) -> None:
        """Money has left the building; the fix is an adjustment run."""
        with pytest.raises(PayrollAlreadyPaidError):
            ensure_payslip_voidable(PayslipStatus.PAID)

    def test_calculated_payslip_can_be_voided(self) -> None:
        ensure_payslip_voidable(PayslipStatus.CALCULATED)


class TestOverlapRules:
    def test_payroll_periods_cannot_overlap(self) -> None:
        with pytest.raises(PayrollPeriodOverlapError):
            ensure_no_period_overlap(
                date(2026, 3, 1),
                date(2026, 3, 31),
                [(date(2026, 3, 15), date(2026, 4, 15))],
            )

    def test_adjacent_periods_are_fine(self) -> None:
        ensure_no_period_overlap(
            date(2026, 4, 1), date(2026, 4, 30), [(date(2026, 3, 1), date(2026, 3, 31))]
        )

    def test_salary_structures_cannot_overlap(self) -> None:
        with pytest.raises(SalaryStructureOverlapError):
            ensure_no_structure_overlap(date(2026, 6, 1), None, [(date(2026, 1, 1), None)])

    def test_an_open_ended_structure_blocks_everything_after_it(self) -> None:
        with pytest.raises(SalaryStructureOverlapError):
            ensure_no_structure_overlap(date(2030, 1, 1), None, [(date(2026, 1, 1), None)])

    def test_a_closed_structure_leaves_room_after_it(self) -> None:
        ensure_no_structure_overlap(date(2026, 7, 1), None, [(date(2026, 1, 1), date(2026, 6, 30))])
