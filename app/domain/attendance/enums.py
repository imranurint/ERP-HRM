"""Attendance enumerations."""

from __future__ import annotations

from enum import StrEnum


class AttendanceStatus(StrEnum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LATE = "LATE"
    HALF_DAY = "HALF_DAY"
    LEAVE = "LEAVE"
    HOLIDAY = "HOLIDAY"
    WEEKEND = "WEEKEND"

    @property
    def is_worked(self) -> bool:
        """Did the employee actually work? Drives attendance-based proration."""
        return self in (
            AttendanceStatus.PRESENT,
            AttendanceStatus.LATE,
            AttendanceStatus.HALF_DAY,
        )

    @property
    def is_payable(self) -> bool:
        """Counts toward paid days.

        Non-working days (weekend, holiday) are payable for a salaried
        employee; only unauthorised absence is not. Whether LEAVE is payable
        depends on the leave type, so that decision is made by the payroll
        calculator, which can see it — not here.
        """
        return self is not AttendanceStatus.ABSENT


class AttendanceSource(StrEnum):
    """Where a record came from. Kept for audit, not for logic.

    A record from a biometric terminal and one keyed in by an HR officer carry
    different weight in a dispute, so the provenance is never overwritten.
    """

    MANUAL = "MANUAL"
    BIOMETRIC = "BIOMETRIC"
    API = "API"
    IMPORT = "IMPORT"

    @property
    def is_self_service(self) -> bool:
        return self in (AttendanceSource.BIOMETRIC, AttendanceSource.API)
