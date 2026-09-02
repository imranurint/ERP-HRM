"""Unit of Work.

One UoW == one database transaction == one HR operation. A payroll run opens a
UoW, does everything (lock the period, allocate the run number, resolve every
employee's salary and attendance, compute and insert every payslip and item,
write the audit log, enqueue the outbox event) and commits once. Any failure
rolls the whole thing back — there is no code path that can leave half a
payroll behind.

Repositories are attached here so services depend on the UoW rather than on a
session, keeping SQLAlchemy out of the application layer's signature.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.infrastructure.database.repositories.attendance import (
    AttendanceRepository,
    ShiftAssignmentRepository,
    ShiftRepository,
)
from app.infrastructure.database.repositories.audit import AuditRepository
from app.infrastructure.database.repositories.employees import (
    EmployeeRepository,
    EmploymentContractRepository,
    LifecycleEventRepository,
)
from app.infrastructure.database.repositories.identity import (
    PermissionRepository,
    RefreshTokenRepository,
    RoleRepository,
    UserRepository,
)
from app.infrastructure.database.repositories.leave import (
    LeaveBalanceRepository,
    LeavePolicyRepository,
    LeaveRequestRepository,
    LeaveTransactionRepository,
    LeaveTypeRepository,
)
from app.infrastructure.database.repositories.organization import (
    CompanyRepository,
    CurrencyRepository,
    DepartmentRepository,
    DesignationRepository,
    HolidayRepository,
    LocationRepository,
)
from app.infrastructure.database.repositories.payroll import (
    PayrollPeriodRepository,
    PayrollRunRepository,
    PayslipRepository,
)
from app.infrastructure.database.repositories.salary import (
    SalaryComponentRepository,
    SalaryStructureRepository,
)
from app.infrastructure.database.repositories.sequences import DocumentSequenceRepository
from app.infrastructure.outbox.repository import OutboxRepository


class UnitOfWork:
    """Transaction scope plus the repositories that share it."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

        # organisation
        self.companies = CompanyRepository(session)
        self.currencies = CurrencyRepository(session)
        self.departments = DepartmentRepository(session)
        self.designations = DesignationRepository(session)
        self.locations = LocationRepository(session)
        self.holidays = HolidayRepository(session)

        # people
        self.employees = EmployeeRepository(session)
        self.contracts = EmploymentContractRepository(session)
        self.lifecycle = LifecycleEventRepository(session)

        # pay configuration
        self.salary_components = SalaryComponentRepository(session)
        self.salary_structures = SalaryStructureRepository(session)

        # attendance
        self.shifts = ShiftRepository(session)
        self.shift_assignments = ShiftAssignmentRepository(session)
        self.attendance = AttendanceRepository(session)

        # leave
        self.leave_types = LeaveTypeRepository(session)
        self.leave_policies = LeavePolicyRepository(session)
        self.leave_requests = LeaveRequestRepository(session)
        self.leave_transactions = LeaveTransactionRepository(session)
        self.leave_balances = LeaveBalanceRepository(session)

        # payroll
        self.payroll_periods = PayrollPeriodRepository(session)
        self.payroll_runs = PayrollRunRepository(session)
        self.payslips = PayslipRepository(session)

        # cross-cutting
        self.sequences = DocumentSequenceRepository(session)
        self.audit = AuditRepository(session)
        self.outbox = OutboxRepository(session)

        # identity
        self.users = UserRepository(session)
        self.roles = RoleRepository(session)
        self.permissions = PermissionRepository(session)
        self.refresh_tokens = RefreshTokenRepository(session)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # An unhandled exception must never leave a partially applied payroll
        # or leave change visible to another transaction.
        if exc_type is not None:
            await self.rollback()

    async def commit(self) -> None:
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise translate_integrity_error(exc) from exc

    async def rollback(self) -> None:
        await self.session.rollback()

    async def flush(self) -> None:
        """Send pending INSERT/UPDATEs so constraints fire before we continue."""
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def execute(self, statement: Any, params: Any = None) -> Any:
        return await self.session.execute(statement, params)

    async def refresh(self, instance: Any, attribute_names: list[str] | None = None) -> None:
        """Reload an instance from the database.

        Needed after writes for columns with a server default or `onupdate`
        (`created_at`, `updated_at`, `version`), which the flush leaves
        expired. Touching one lazily later would attempt blocking IO from
        async code and raise `MissingGreenlet`.
        """
        await self.session.refresh(instance, attribute_names=attribute_names)


#: Database constraint name → the domain error it really means. Translating
#: here means a race that slips past an application check still surfaces as a
#: precise business error instead of a 500.
_CONSTRAINT_ERRORS: dict[str, tuple[str, str]] = {
    # --- organisation -------------------------------------------------------
    "uq_departments_company_id_department_code": (
        "DUPLICATE_RESOURCE",
        "That department code already exists for this company.",
    ),
    "uq_designations_company_id_designation_code": (
        "DUPLICATE_RESOURCE",
        "That designation code already exists for this company.",
    ),
    "uq_locations_company_id_location_code": (
        "DUPLICATE_RESOURCE",
        "That location code already exists for this company.",
    ),
    # --- people -------------------------------------------------------------
    "uq_employees_company_id_employee_number": (
        "DUPLICATE_EMPLOYEE_NUMBER",
        "That employee number already exists for this company.",
    ),
    "uq_employees_company_id_work_email": (
        "DUPLICATE_WORK_EMAIL",
        "That work email address is already in use in this company.",
    ),
    "ix_employees_user": (
        "DUPLICATE_RESOURCE",
        "That user account is already linked to another employee.",
    ),
    "ck_employees_is_active_matches_status": (
        "INVALID_EMPLOYMENT_STATUS",
        "The active flag does not match the employment status.",
    ),
    "ck_employees_terminal_status_has_date": (
        "INVALID_EMPLOYMENT_STATUS",
        "A terminated or resigned employee must have a termination date.",
    ),
    "uq_employment_contracts_one_active": (
        "ACTIVE_CONTRACT_EXISTS",
        "This employee already has an active employment contract.",
    ),
    "uq_employment_contracts_company_id_contract_number": (
        "DUPLICATE_RESOURCE",
        "That contract number already exists for this company.",
    ),
    # --- salary -------------------------------------------------------------
    "uq_salary_components_company_id_component_code": (
        "DUPLICATE_RESOURCE",
        "That salary component code already exists for this company.",
    ),
    "ex_employee_salary_structures_no_overlap": (
        "SALARY_STRUCTURE_OVERLAP",
        "An employee's salary structures must not overlap in time.",
    ),
    # --- attendance ---------------------------------------------------------
    "uq_attendance_records_employee_id_attendance_date": (
        "DUPLICATE_ATTENDANCE",
        "An attendance record already exists for that employee and date.",
    ),
    "uq_shifts_company_id_shift_code": (
        "DUPLICATE_RESOURCE",
        "That shift code already exists for this company.",
    ),
    "ex_employee_shift_assignments_no_overlap": (
        "CONFLICT",
        "An employee's shift assignments must not overlap in time.",
    ),
    # --- leave --------------------------------------------------------------
    "uq_leave_types_company_id_leave_code": (
        "DUPLICATE_RESOURCE",
        "That leave code already exists for this company.",
    ),
    "ex_employee_leave_policies_no_overlap": (
        "CONFLICT",
        "An employee's leave policy assignments must not overlap in time.",
    ),
    "ck_leave_balances_closing_balance_derivation": (
        "CONFLICT",
        "Leave balance arithmetic is inconsistent; the projection was not updated atomically.",
    ),
    # --- payroll ------------------------------------------------------------
    "ex_payroll_periods_no_overlap": (
        "PAYROLL_PERIOD_OVERLAP",
        "Payroll periods for a company must not overlap.",
    ),
    "uq_payroll_periods_company_id_period_name": (
        "DUPLICATE_PAYROLL_PERIOD",
        "A payroll period with that name already exists for this company.",
    ),
    "uq_payroll_runs_company_id_run_number": (
        "DUPLICATE_RESOURCE",
        "That payroll run number already exists for this company.",
    ),
    "uq_payslips_payroll_run_id_employee_id": (
        "DUPLICATE_RESOURCE",
        "This employee already has a payslip in that payroll run.",
    ),
    "ck_payslips_net_salary_derivation": (
        "CONFLICT",
        "Net salary must equal gross salary minus total deductions.",
    ),
    # --- identity -----------------------------------------------------------
    "uq_users_email": (
        "EMAIL_ALREADY_REGISTERED",
        "An account with this email address already exists.",
    ),
}


def translate_integrity_error(exc: IntegrityError) -> ConflictError:
    detail = str(getattr(exc.orig, "args", ("",))[0] if exc.orig else exc)
    for constraint, (code, message) in _CONSTRAINT_ERRORS.items():
        if constraint in detail:
            return ConflictError(message, code=code, details={"constraint": constraint})
    return ConflictError(
        "The operation violated a database constraint.",
        code="CONSTRAINT_VIOLATION",
        details={"constraint": None},
    )
