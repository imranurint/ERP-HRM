"""Attendance domain errors."""

from __future__ import annotations

from app.core.exceptions import ConflictError, NotFoundError, ValidationError


class AttendanceNotFoundError(NotFoundError):
    code = "ATTENDANCE_NOT_FOUND"
    message = "No attendance record exists for that employee and date."


class DuplicateAttendanceError(ConflictError):
    code = "DUPLICATE_ATTENDANCE"
    message = "An attendance record already exists for that employee and date."


class AlreadyCheckedInError(ConflictError):
    code = "ALREADY_CHECKED_IN"
    message = "This employee has already checked in today."


class NotCheckedInError(ConflictError):
    code = "NOT_CHECKED_IN"
    message = "Cannot check out before checking in."


class AlreadyCheckedOutError(ConflictError):
    code = "ALREADY_CHECKED_OUT"
    message = "This employee has already checked out today."


class InvalidAttendanceWindowError(ValidationError):
    code = "INVALID_ATTENDANCE_WINDOW"
    message = "Check-out must be after check-in."


class AttendanceLockedError(ConflictError):
    """Attendance inside an approved or paid payroll period is frozen.

    Editing it after the fact would silently invalidate a payslip that has
    already been approved or paid.
    """

    code = "ATTENDANCE_LOCKED"
    message = "Attendance for a processed payroll period cannot be modified."


class ShiftNotFoundError(NotFoundError):
    code = "SHIFT_NOT_FOUND"
    message = "The requested shift does not exist."


class NoShiftAssignedError(ConflictError):
    code = "NO_SHIFT_ASSIGNED"
    message = "This employee has no shift assigned on that date."


class InvalidShiftError(ValidationError):
    code = "INVALID_SHIFT"
    message = "The shift definition is not valid."
