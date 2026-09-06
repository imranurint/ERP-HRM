"""Payroll domain errors."""

from __future__ import annotations

from app.core.exceptions import ConflictError, NotFoundError, ValidationError


class PayrollPeriodNotFoundError(NotFoundError):
    code = "PAYROLL_PERIOD_NOT_FOUND"
    message = "The requested payroll period does not exist."


class PayrollPeriodClosedError(ConflictError):
    code = "PAYROLL_PERIOD_CLOSED"
    message = "This payroll period is no longer open for processing."


class PayrollPeriodOverlapError(ConflictError):
    code = "PAYROLL_PERIOD_OVERLAP"
    message = "Payroll periods for a company must not overlap."


class DuplicatePayrollPeriodError(ConflictError):
    code = "DUPLICATE_PAYROLL_PERIOD"
    message = "A payroll period with that name already exists for this company."


class PayrollRunNotFoundError(NotFoundError):
    code = "PAYROLL_RUN_NOT_FOUND"
    message = "The requested payroll run does not exist."


class PayrollAlreadyApprovedError(ConflictError):
    code = "PAYROLL_ALREADY_APPROVED"
    message = "Approved payroll cannot be modified."


class PayrollAlreadyPaidError(ConflictError):
    code = "PAYROLL_ALREADY_PAID"
    message = "Paid payroll cannot be modified."


class PayrollNotCalculatedError(ConflictError):
    code = "PAYROLL_NOT_CALCULATED"
    message = "This payroll run has not been calculated yet."


class PayrollNotApprovedError(ConflictError):
    code = "PAYROLL_NOT_APPROVED"
    message = "Only an approved payroll run can be paid."


class PayrollRunInProgressError(ConflictError):
    code = "PAYROLL_RUN_IN_PROGRESS"
    message = "Another payroll run for this period is already in progress."


class PayrollEmptyError(ConflictError):
    code = "PAYROLL_EMPTY"
    message = "This payroll run produced no payslips."


class PayslipNotFoundError(NotFoundError):
    code = "PAYSLIP_NOT_FOUND"
    message = "The requested payslip does not exist."


class PayslipCannotBeModifiedError(ConflictError):
    code = "PAYSLIP_CANNOT_BE_MODIFIED"
    message = "An approved or paid payslip cannot be modified."


class SalaryStructureNotFoundError(NotFoundError):
    code = "SALARY_STRUCTURE_NOT_FOUND"
    message = "No salary structure is in effect for that employee and date."


class SalaryStructureInvalidError(ValidationError):
    code = "SALARY_STRUCTURE_INVALID"
    message = "The salary structure is not valid."


class SalaryStructureOverlapError(ConflictError):
    code = "SALARY_STRUCTURE_OVERLAP"
    message = "An employee's salary structures must not overlap in time."


class SalaryComponentNotFoundError(NotFoundError):
    code = "SALARY_COMPONENT_NOT_FOUND"
    message = "The requested salary component does not exist."


class SalaryComponentInvalidError(ValidationError):
    code = "SALARY_COMPONENT_INVALID"
    message = "The salary component configuration is not valid."


class UnknownFormulaError(ValidationError):
    """A FORMULA component names a calculator that is not registered.

    Formulas are code, not user-supplied expressions — see
    `app.modules.payroll.domain.formulas` for why.
    """

    code = "UNKNOWN_PAYROLL_FORMULA"
    message = "That payroll formula is not registered."


class NegativeNetPayError(ConflictError):
    """Deductions exceeded earnings.

    Refused rather than clamped: a negative net is always a configuration
    error, and silently paying zero would hide it for months.
    """

    code = "NEGATIVE_NET_PAY"
    message = "Deductions exceed earnings; net pay would be negative."
