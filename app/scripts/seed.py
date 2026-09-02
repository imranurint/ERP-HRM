"""Seed a demo company with a complete, payable configuration.

    python -m app.scripts.seed

**Idempotent.** Running it twice makes no second copy of anything — every
step looks the row up first. That matters because a seed is often run against
a shared development database by more than one person.

No real passwords are committed. If `SEED_*_PASSWORD` is unset, a password is
generated, printed once to stdout, and never logged or stored in plaintext.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from datetime import date, time, timedelta
from decimal import Decimal
from uuid import UUID

from app.application.context import ActorContext
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.domain.attendance.enums import AttendanceStatus
from app.domain.employees.enums import ContractStatus, ContractType, EmploymentStatus, Gender
from app.domain.identity.permissions import Role
from app.domain.leave.enums import LeaveTransactionType
from app.domain.payroll.enums import (
    CalculationType,
    PayrollPeriodStatus,
    PercentageBase,
    SalaryComponentType,
    SalaryStructureStatus,
)
from app.infrastructure.database.models.attendance import (
    AttendanceRecord,
    EmployeeShiftAssignment,
    Shift,
)
from app.infrastructure.database.models.employee import Employee, EmploymentContract
from app.infrastructure.database.models.identity import User
from app.infrastructure.database.models.leave import (
    EmployeeLeavePolicy,
    LeavePolicy,
    LeavePolicyRule,
    LeaveTransaction,
    LeaveType,
)
from app.infrastructure.database.models.organization import (
    Company,
    Currency,
    Department,
    Designation,
    Holiday,
    Location,
)
from app.infrastructure.database.models.payroll import PayrollPeriod
from app.infrastructure.database.models.salary import (
    EmployeeSalaryComponent,
    EmployeeSalaryStructure,
    SalaryComponent,
)
from app.infrastructure.database.session import dispose_engine, session_scope
from app.infrastructure.database.unit_of_work import UnitOfWork
from app.scripts.create_admin import ensure_roles_and_permissions

logger = get_logger("seed")

COMPANY_NAME = "Demo Company Ltd"
CURRENCY = "GBP"
YEAR = 2026

DEPARTMENTS = [
    ("HR", "Human Resources"),
    ("FIN", "Finance"),
    ("ENG", "Engineering"),
    ("SALES", "Sales"),
    ("OPS", "Operations"),
]

DESIGNATIONS = [
    ("CEO", "Chief Executive Officer", 10),
    ("HRM", "HR Manager", 7),
    ("SWE", "Software Engineer", 5),
    ("ACC", "Accountant", 5),
    ("SE", "Sales Executive", 4),
]

CURRENCIES = [
    ("GBP", "Pound Sterling", 2),
    ("USD", "US Dollar", 2),
    ("EUR", "Euro", 2),
    ("JPY", "Japanese Yen", 0),
]

COMPONENTS: list[tuple[str, str, SalaryComponentType, CalculationType, int, bool, str]] = [
    # (code, name, type, calculation, sequence, taxable, gl_account_code)
    (
        "HOUSING",
        "Housing Allowance",
        SalaryComponentType.EARNING,
        CalculationType.PERCENTAGE,
        10,
        True,
        "6010",
    ),
    (
        "TRANSPORT",
        "Transport Allowance",
        SalaryComponentType.EARNING,
        CalculationType.FIXED,
        20,
        True,
        "6020",
    ),
    (
        "OVERTIME",
        "Overtime",
        SalaryComponentType.EARNING,
        CalculationType.FORMULA,
        30,
        True,
        "6030",
    ),
    (
        "PENSION",
        "Pension (employee)",
        SalaryComponentType.DEDUCTION,
        CalculationType.PERCENTAGE,
        40,
        False,
        "2200",
    ),
    (
        "TAX",
        "Income Tax",
        SalaryComponentType.DEDUCTION,
        CalculationType.FORMULA,
        50,
        False,
        "2210",
    ),
    (
        "NI_ER",
        "Employer NI",
        SalaryComponentType.EMPLOYER_CONTRIBUTION,
        CalculationType.PERCENTAGE,
        60,
        False,
        "6040",
    ),
]

LEAVE_TYPES = [
    ("ANNUAL", "Annual Leave", True, Decimal("25.00"), Decimal("5.00")),
    ("SICK", "Sick Leave", True, Decimal("10.00"), None),
    ("UNPAID", "Unpaid Leave", False, None, None),
]

EMPLOYEES = [
    ("Ada", "Lovelace", "ada.lovelace@demo.example", "ENG", "SWE", Decimal("4500.00")),
    ("Grace", "Hopper", "grace.hopper@demo.example", "ENG", "SWE", Decimal("4800.00")),
    ("Alan", "Turing", "alan.turing@demo.example", "OPS", "CEO", Decimal("9000.00")),
    ("Katherine", "Johnson", "katherine.johnson@demo.example", "FIN", "ACC", Decimal("3800.00")),
    ("Mary", "Jackson", "mary.jackson@demo.example", "HR", "HRM", Decimal("4200.00")),
]

#: role → (email, settings attribute holding a password override)
SEED_USERS: list[tuple[Role, str, str]] = [
    (Role.HR_MANAGER, "hr.manager@demo.example", "seed_hr_manager_password"),
    (Role.PAYROLL_MANAGER, "payroll.manager@demo.example", "seed_payroll_manager_password"),
    (Role.EMPLOYEE, "ada.lovelace@demo.example", "seed_employee_password"),
]


async def seed() -> None:
    generated: list[tuple[str, str]] = []

    async with session_scope() as session:
        uow = UnitOfWork(session)
        actor = ActorContext()

        roles = await ensure_roles_and_permissions(uow)

        # --- currencies -------------------------------------------------
        for code, name, places in CURRENCIES:
            if await uow.currencies.get_by_code(code) is None:
                uow.currencies.add(
                    Currency(
                        currency_code=code,
                        currency_name=name,
                        decimal_places=places,
                        is_active=True,
                    )
                )
        await uow.flush()

        # --- company ----------------------------------------------------
        company = await _find_company(uow, COMPANY_NAME)
        if company is None:
            company = Company(
                name=COMPANY_NAME,
                legal_name=f"{COMPANY_NAME}.",
                registration_number="DEMO-0000001",
                email="hello@demo.example",
                timezone="Europe/London",
                base_currency_code=CURRENCY,
                is_active=True,
            )
            uow.companies.add(company)
            await uow.flush()
        company_id = company.id

        # --- organisation structure -------------------------------------
        departments: dict[str, Department] = {}
        for code, name in DEPARTMENTS:
            existing = await uow.departments.get_by_code(company_id, code)
            if existing is None:
                existing = Department(
                    company_id=company_id, department_code=code, name=name, is_active=True
                )
                uow.departments.add(existing)
            departments[code] = existing

        designations: dict[str, Designation] = {}
        for code, name, grade in DESIGNATIONS:
            existing_d = await uow.designations.get_by_code(company_id, code)
            if existing_d is None:
                existing_d = Designation(
                    company_id=company_id,
                    designation_code=code,
                    name=name,
                    grade_level=grade,
                    is_active=True,
                )
                uow.designations.add(existing_d)
            designations[code] = existing_d

        location = await uow.locations.get_by_code(company_id, "HQ")
        if location is None:
            location = Location(
                company_id=company_id,
                location_code="HQ",
                name="London Head Office",
                timezone="Europe/London",
                is_active=True,
            )
            uow.locations.add(location)
        await uow.flush()

        # --- holidays ---------------------------------------------------
        existing_holidays = await uow.holidays.dates_between(
            company_id, date(YEAR, 1, 1), date(YEAR, 12, 31)
        )
        for holiday_date, name in [
            (date(YEAR, 1, 1), "New Year's Day"),
            (date(YEAR, 12, 25), "Christmas Day"),
            (date(YEAR, 12, 26), "Boxing Day"),
        ]:
            if holiday_date not in existing_holidays:
                uow.holidays.add(
                    Holiday(
                        company_id=company_id,
                        holiday_date=holiday_date,
                        name=name,
                        is_active=True,
                    )
                )

        # --- shift ------------------------------------------------------
        shift = await uow.shifts.get_by_code(company_id, "DAY")
        if shift is None:
            shift = Shift(
                company_id=company_id,
                shift_code="DAY",
                name="Standard Day (09:00–17:00)",
                start_time=time(9, 0),
                end_time=time(17, 0),
                grace_minutes=10,
                working_hours=Decimal("8.00"),
                is_active=True,
            )
            uow.shifts.add(shift)
        await uow.flush()

        # --- salary components ------------------------------------------
        components: dict[str, SalaryComponent] = {}
        for code, name, kind, calc, sequence, taxable, gl_account in COMPONENTS:
            existing_c = await uow.salary_components.get_by_code(company_id, code)
            if existing_c is None:
                existing_c = SalaryComponent(
                    company_id=company_id,
                    component_code=code,
                    name=name,
                    component_type=kind,
                    calculation_type=calc,
                    percentage_base=(
                        PercentageBase.GROSS_EARNINGS
                        if code in {"PENSION", "NI_ER"}
                        else PercentageBase.BASE_SALARY
                    ),
                    formula_key=_formula_for(code),
                    sequence=sequence,
                    is_taxable=taxable,
                    gl_account_code=gl_account,
                    is_active=True,
                )
                uow.salary_components.add(existing_c)
            components[code] = existing_c

        # --- leave types and policy -------------------------------------
        leave_types: dict[str, LeaveType] = {}
        for code, name, is_paid, annual_limit, carry in LEAVE_TYPES:
            existing_l = await uow.leave_types.get_by_code(company_id, code)
            if existing_l is None:
                existing_l = LeaveType(
                    company_id=company_id,
                    leave_code=code,
                    name=name,
                    is_paid=is_paid,
                    annual_limit=annual_limit,
                    carry_forward_limit=carry,
                    requires_approval=True,
                    excludes_non_working_days=True,
                    allows_negative_balance=False,
                    is_active=True,
                )
                uow.leave_types.add(existing_l)
            leave_types[code] = existing_l
        await uow.flush()

        policy = await _find_policy(uow, company_id, "Standard UK Policy")
        if policy is None:
            policy = LeavePolicy(
                company_id=company_id,
                name="Standard UK Policy",
                description="25 days annual leave, 10 days sick, unpaid on request.",
                is_active=True,
            )
            uow.leave_policies.add(policy)
            await uow.flush()
            for code, entitlement, carry_allowed, carry_limit in [
                ("ANNUAL", Decimal("25.00"), True, Decimal("5.00")),
                ("SICK", Decimal("10.00"), False, None),
                ("UNPAID", Decimal("0.00"), False, None),
            ]:
                session.add(
                    LeavePolicyRule(
                        policy_id=policy.id,
                        leave_type_id=leave_types[code].id,
                        annual_entitlement=entitlement,
                        carry_forward_allowed=carry_allowed,
                        carry_forward_limit=carry_limit,
                        minimum_service_days=0,
                    )
                )
        await uow.flush()

        # --- employees --------------------------------------------------
        hire_date = date(YEAR, 1, 6)
        employees: dict[str, Employee] = {}
        for first, last, email, dept, desig, salary in EMPLOYEES:
            employee = await uow.employees.get_by_work_email(company_id, email)
            if employee is None:
                number = await uow.sequences.allocate_employee_number(company_id, YEAR)
                employee = Employee(
                    company_id=company_id,
                    employee_number=number,
                    first_name=first,
                    last_name=last,
                    work_email=email,
                    gender=Gender.UNDISCLOSED,
                    hire_date=hire_date,
                    employment_status=EmploymentStatus.ACTIVE,
                    department_id=departments[dept].id,
                    designation_id=designations[desig].id,
                    location_id=location.id,
                    is_active=True,
                )
                uow.employees.add(employee)
                await uow.flush()

                session.add(
                    EmploymentContract(
                        company_id=company_id,
                        employee_id=employee.id,
                        contract_number=await uow.sequences.allocate_contract_number(
                            company_id, YEAR
                        ),
                        contract_type=ContractType.PERMANENT,
                        start_date=hire_date,
                        working_hours_per_week=40,
                        status=ContractStatus.ACTIVE,
                    )
                )
                session.add(
                    EmployeeShiftAssignment(
                        company_id=company_id,
                        employee_id=employee.id,
                        shift_id=shift.id,
                        effective_from=hire_date,
                    )
                )
                session.add(
                    EmployeeLeavePolicy(
                        company_id=company_id,
                        employee_id=employee.id,
                        leave_policy_id=policy.id,
                        effective_from=hire_date,
                    )
                )
                await _grant_leave(uow, company_id, employee.id, leave_types, actor)
                await _build_salary(uow, session, company_id, employee.id, salary, components)
            employees[email] = employee
        await uow.flush()

        # --- an open payroll period and a little attendance -------------
        period = await uow.payroll_periods.get_by_name(company_id, f"{YEAR}-03")
        if period is None:
            period = PayrollPeriod(
                company_id=company_id,
                period_name=f"{YEAR}-03",
                start_date=date(YEAR, 3, 1),
                end_date=date(YEAR, 3, 31),
                pay_date=date(YEAR, 4, 5),
                status=PayrollPeriodStatus.OPEN,
            )
            uow.payroll_periods.add(period)
        await _seed_attendance(uow, company_id, employees, shift.id)

        # --- users ------------------------------------------------------
        for role, email, password_attr in SEED_USERS:
            if await uow.users.get_by_email(email) is not None:
                continue
            configured = getattr(settings, password_attr, None)
            password = configured or secrets.token_urlsafe(18)
            if not configured:
                generated.append((email, password))
            user = User(
                email=email,
                hashed_password=hash_password(password),
                full_name=email.split("@")[0].replace(".", " ").title(),
                is_active=True,
                is_superuser=False,
            )
            user.roles.append(roles[role])
            uow.users.add(user)
            await uow.flush()

            # Link the Employee-role login to its employee record so
            # self-service scoping works out of the box.
            employee = employees.get(email)
            if employee is not None:
                employee.user_id = user.id

        await uow.commit()

    print(
        f"Seeded {COMPANY_NAME}: {len(EMPLOYEES)} employees, "
        f"{len(DEPARTMENTS)} departments, {len(COMPONENTS)} salary components."
    )
    if generated:
        print("\nGenerated passwords (shown once, never stored in plaintext):")
        for email, password in generated:
            print(f"  {email}: {password}")
    print(
        f"\nAdmin: create with `make create-admin email={settings.seed_admin_email}`"
        if settings.seed_admin_email
        else ""
    )


def _formula_for(code: str) -> str | None:
    return {"OVERTIME": "overtime_hourly_1_5x", "TAX": "progressive_tax_3_band"}.get(code)


async def _find_company(uow: UnitOfWork, name: str) -> Company | None:
    from sqlalchemy import select

    stmt = select(Company).where(Company.name == name)
    return (await uow.session.execute(stmt)).scalar_one_or_none()


async def _find_policy(uow: UnitOfWork, company_id: UUID, name: str) -> LeavePolicy | None:
    from sqlalchemy import select

    stmt = select(LeavePolicy).where(LeavePolicy.company_id == company_id, LeavePolicy.name == name)
    return (await uow.session.execute(stmt)).scalar_one_or_none()


async def _grant_leave(
    uow: UnitOfWork,
    company_id: UUID,
    employee_id: UUID,
    leave_types: dict[str, LeaveType],
    actor: ActorContext,
) -> None:
    """Grant the year's entitlement as transactions, then project the balance.

    Written the same way the running application writes it — a transaction
    first, the projection second — so seeded data passes the rebuild check.
    """
    for code, days in [("ANNUAL", Decimal("25.00")), ("SICK", Decimal("10.00"))]:
        leave_type = leave_types[code]
        uow.leave_transactions.add(
            LeaveTransaction(
                company_id=company_id,
                employee_id=employee_id,
                leave_type_id=leave_type.id,
                year=YEAR,
                transaction_type=LeaveTransactionType.ENTITLEMENT,
                days=days,
                source_type="POLICY_GRANT",
                notes="Seeded annual entitlement",
            )
        )
        await uow.leave_balances.apply_movement(
            company_id=company_id,
            employee_id=employee_id,
            leave_type_id=leave_type.id,
            year=YEAR,
            earned_delta=days,
        )


async def _build_salary(
    uow: UnitOfWork,
    session: object,
    company_id: UUID,
    employee_id: UUID,
    base_salary: Decimal,
    components: dict[str, SalaryComponent],
) -> None:
    structure = EmployeeSalaryStructure(
        company_id=company_id,
        employee_id=employee_id,
        effective_from=date(YEAR, 1, 6),
        currency_code=CURRENCY,
        base_salary=base_salary,
        prorate_base_salary=True,
        status=SalaryStructureStatus.ACTIVE,
    )
    uow.salary_structures.add(structure)
    await uow.flush()

    lines = [
        ("HOUSING", Decimal("0"), Decimal("15.0000")),
        ("TRANSPORT", Decimal("150.00"), None),
        ("OVERTIME", Decimal("0"), None),
        ("PENSION", Decimal("0"), Decimal("5.0000")),
        ("TAX", Decimal("0"), None),
        ("NI_ER", Decimal("0"), Decimal("13.8000")),
    ]
    for code, amount, percentage in lines:
        uow.session.add(
            EmployeeSalaryComponent(
                salary_structure_id=structure.id,
                salary_component_id=components[code].id,
                amount=amount,
                percentage=percentage,
            )
        )
    await uow.flush()


async def _seed_attendance(
    uow: UnitOfWork, company_id: UUID, employees: dict[str, Employee], shift_id: UUID
) -> None:
    """A handful of March attendance rows, including one absence.

    Enough for a payroll run to demonstrate proration without pretending to be
    a full month of biometric data.
    """
    from app.core.dates import combine_shift_datetime

    ada = employees.get("ada.lovelace@demo.example")
    if ada is None:
        return
    if await uow.attendance.get_for_day(ada.id, date(YEAR, 3, 2)) is not None:
        return

    for offset, status in [(0, AttendanceStatus.PRESENT), (1, AttendanceStatus.ABSENT)]:
        day = date(YEAR, 3, 2) + timedelta(days=offset)
        check_in = (
            combine_shift_datetime(day, time(9, 5), timezone_name="Europe/London")
            if status is AttendanceStatus.PRESENT
            else None
        )
        check_out = (
            combine_shift_datetime(day, time(17, 30), timezone_name="Europe/London")
            if status is AttendanceStatus.PRESENT
            else None
        )
        uow.attendance.add(
            AttendanceRecord(
                company_id=company_id,
                employee_id=ada.id,
                attendance_date=day,
                shift_id=shift_id,
                check_in=check_in,
                check_out=check_out,
                status=status,
                working_minutes=505 if check_in else 0,
                overtime_minutes=25 if check_in else 0,
                late_minutes=0,
                early_leave_minutes=0,
            )
        )


async def main() -> int:
    configure_logging()
    try:
        await seed()
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
