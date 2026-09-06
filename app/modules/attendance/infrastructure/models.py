"""Shifts and attendance records."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.modules.attendance.domain.enums import AttendanceSource, AttendanceStatus
from app.shared.database.base import (
    ActorTrackingMixin,
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from app.shared.database.types import status_enum


class Shift(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """A working pattern.

    **Overnight shifts are supported.** `end_time <= start_time` means the
    shift finishes on the following calendar day; the record is still filed
    under the day it *started*, so a night worker's Monday shift is one
    attendance row, not two halves.
    """

    __tablename__ = "shifts"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    shift_code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    start_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    end_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    #: Lateness inside this window is exactly zero minutes late, so a two
    #: minute delay never becomes a payroll deduction.
    grace_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    working_hours: Mapped[Decimal] = mapped_column(
        Numeric(5, 2, asdecimal=True), nullable=False, server_default=text("8")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("company_id", "shift_code", name="uq_shifts_company_id_shift_code"),
        CheckConstraint("grace_minutes >= 0 AND grace_minutes <= 240", name="grace_minutes_range"),
        CheckConstraint("working_hours > 0 AND working_hours <= 24", name="working_hours_range"),
        CheckConstraint("length(trim(shift_code)) > 0", name="shift_code_not_blank"),
        Index("ix_shifts_company_active", "company_id", "is_active"),
    )


class EmployeeShiftAssignment(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """Which shift an employee works, over a date range.

    Assignments for one employee must not overlap — enforced by a GiST
    exclusion constraint, because "which shift were they on?" needs exactly
    one answer for any given day.
    """

    __tablename__ = "employee_shift_assignments"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    shift_id: Mapped[UUID] = mapped_column(
        ForeignKey("shifts.id", ondelete="RESTRICT"), nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)

    shift: Mapped[Shift] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="assignment_date_order",
        ),
        Index("ix_employee_shift_assignments_employee", "employee_id", "effective_from"),
        Index("ix_employee_shift_assignments_company", "company_id"),
    )


class AttendanceRecord(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """One employee, one day.

    Every derived minute figure (`working_minutes`, `late_minutes`, …) is
    computed by `app.modules.attendance.domain.rules` from the raw check-in/check-out
    pair and the shift, then stored. Storing the derivation rather than
    recomputing it on read means an amended shift definition cannot silently
    rewrite last year's attendance.
    """

    __tablename__ = "attendance_records"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    attendance_date: Mapped[date] = mapped_column(Date, nullable=False)
    shift_id: Mapped[UUID | None] = mapped_column(ForeignKey("shifts.id", ondelete="SET NULL"))

    check_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    check_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[AttendanceStatus] = mapped_column(
        status_enum(AttendanceStatus, "attendance_status", 16), nullable=False
    )
    working_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    overtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    late_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    early_leave_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    source: Mapped[AttendanceSource] = mapped_column(
        status_enum(AttendanceSource, "attendance_source", 16),
        nullable=False,
        default=AttendanceSource.MANUAL,
    )
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "employee_id",
            "attendance_date",
            name="uq_attendance_records_employee_id_attendance_date",
        ),
        CheckConstraint(
            "check_out IS NULL OR check_in IS NULL OR check_out > check_in",
            name="check_out_after_check_in",
        ),
        # A check-out without a check-in is not a partial record, it is
        # corrupt data — refuse it at the database.
        CheckConstraint(
            "check_out IS NULL OR check_in IS NOT NULL", name="check_out_requires_check_in"
        ),
        CheckConstraint(
            "working_minutes >= 0 AND overtime_minutes >= 0 "
            "AND late_minutes >= 0 AND early_leave_minutes >= 0",
            name="minutes_non_negative",
        ),
        CheckConstraint("working_minutes <= 1440", name="working_minutes_within_day"),
        Index("ix_attendance_records_company_date", "company_id", "attendance_date"),
        Index("ix_attendance_records_employee_date", "employee_id", "attendance_date"),
        Index("ix_attendance_records_company_status", "company_id", "status"),
        # Supports "who is still checked in?" without scanning the table.
        Index(
            "ix_attendance_records_open",
            "company_id",
            "attendance_date",
            postgresql_where=text("check_in IS NOT NULL AND check_out IS NULL"),
        ),
    )
