"""Test data builders.

`DemoData.create` builds a complete, *payable* company in one call: currencies,
org structure, a shift, salary components, leave types and policy, and five
employees with active contracts and salary structures. Integration tests start
from a realistic configuration rather than assembling one each time.

Objects are returned as plain attributes so a test can reach for exactly what
it needs without a second query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.employees.enums import ContractStatus, ContractType, EmploymentStatus
from app.domain.leave.enums import LeaveTransactionType
from app.domain.payroll.enums import (
    CalculationType,
    PercentageBase,
    SalaryComponentType,
    SalaryStructureStatus,
)
from app.infrastructure.database.models.attendance import EmployeeShiftAssignment, Shift
from app.infrastructure.database.models.employee import Employee, EmploymentContract
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
from app.infrastructure.database.models.salary import (
    EmployeeSalaryComponent,
    EmployeeSalaryStructure,
    SalaryComponent,
)
from app.infrastructure.database.unit_of_work import UnitOfWork

YEAR = 2026
HIRE_DATE = date(YEAR, 1, 5)
CURRENCY = "GBP"


@dataclass
class DemoData:
    """Everything a test needs, already committed."""

    company: Company
    departments: dict[str, Department]
    designations: dict[str, Designation]
    location: Location
    shift: Shift
    components: dict[str, SalaryComponent]
    leave_types: dict[str, LeaveType]
    policy: LeavePolicy
    employees: list[Employee] = field(default_factory=list)
    structures: dict[UUID, EmployeeSalaryStructure] = field(default_factory=dict)

    @property
    def company_id(self) -> UUID:
        return self.company.id

    @property
    def employee(self) -> Employee:
        """The first employee — the one most tests act on."""
        return self.employees[0]

    @property
    def other_employee(self) -> Employee:
        """A second employee, for cross-employee and approval tests."""
        return self.employees[1]

    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        *,
        employee_count: int = 3,
        base_salary: Decimal = Decimal("4000.00"),
    ) -> DemoData:
        uow = UnitOfWork(session)

        for code, name, places in [
            ("GBP", "Pound Sterling", 2),
            ("USD", "US Dollar", 2),
            ("JPY", "Japanese Yen", 0),
        ]:
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

        company = Company(
            name="Test Company Ltd",
            legal_name="Test Company Ltd.",
            timezone="Europe/London",
            base_currency_code=CURRENCY,
            is_active=True,
        )
        uow.companies.add(company)
        await uow.flush()

        departments = {}
        for code, name in [("ENG", "Engineering"), ("HR", "Human Resources")]:
            department = Department(
                company_id=company.id, department_code=code, name=name, is_active=True
            )
            uow.departments.add(department)
            departments[code] = department

        designations = {}
        for code, name, grade in [("SWE", "Software Engineer", 5), ("SM", "Senior Manager", 8)]:
            designation = Designation(
                company_id=company.id,
                designation_code=code,
                name=name,
                grade_level=grade,
                is_active=True,
            )
            uow.designations.add(designation)
            designations[code] = designation

        location = Location(
            company_id=company.id,
            location_code="HQ",
            name="Head Office",
            timezone="Europe/London",
            is_active=True,
        )
        uow.locations.add(location)

        shift = Shift(
            company_id=company.id,
            shift_code="DAY",
            name="Standard Day",
            start_time=time(9, 0),
            end_time=time(17, 0),
            grace_minutes=10,
            working_hours=Decimal("8.00"),
            is_active=True,
        )
        uow.shifts.add(shift)

        # A holiday inside March, so tests can prove it is excluded from both
        # leave day counts and payroll working days.
        uow.holidays.add(
            Holiday(
                company_id=company.id,
                holiday_date=date(YEAR, 3, 17),
                name="Test Holiday",
                is_active=True,
            )
        )
        await uow.flush()

        components = await cls._components(uow, company.id)
        leave_types = await cls._leave_types(uow, company.id)
        policy = await cls._policy(uow, session, company.id, leave_types)

        employees: list[Employee] = []
        structures: dict[UUID, EmployeeSalaryStructure] = {}
        names = [("Ada", "Lovelace"), ("Grace", "Hopper"), ("Alan", "Turing"), ("Mary", "Jackson")]

        for index in range(employee_count):
            first, last = names[index % len(names)]
            number = await uow.sequences.allocate_employee_number(company.id, YEAR)
            employee = Employee(
                company_id=company.id,
                employee_number=number,
                first_name=first,
                last_name=f"{last}{index}",
                work_email=f"{first.lower()}.{last.lower()}{index}@test.example",
                hire_date=HIRE_DATE,
                employment_status=EmploymentStatus.ACTIVE,
                department_id=departments["ENG"].id,
                designation_id=designations["SWE"].id,
                location_id=location.id,
                is_active=True,
            )
            uow.employees.add(employee)
            await uow.flush()

            session.add(
                EmploymentContract(
                    company_id=company.id,
                    employee_id=employee.id,
                    contract_number=await uow.sequences.allocate_contract_number(company.id, YEAR),
                    contract_type=ContractType.PERMANENT,
                    start_date=HIRE_DATE,
                    working_hours_per_week=40,
                    status=ContractStatus.ACTIVE,
                )
            )
            session.add(
                EmployeeShiftAssignment(
                    company_id=company.id,
                    employee_id=employee.id,
                    shift_id=shift.id,
                    effective_from=HIRE_DATE,
                )
            )
            session.add(
                EmployeeLeavePolicy(
                    company_id=company.id,
                    employee_id=employee.id,
                    leave_policy_id=policy.id,
                    effective_from=HIRE_DATE,
                )
            )
            await cls._grant_leave(uow, company.id, employee.id, leave_types)
            structures[employee.id] = await cls._salary(
                uow, session, company.id, employee.id, base_salary, components
            )
            employees.append(employee)

        await uow.commit()

        return cls(
            company=company,
            departments=departments,
            designations=designations,
            location=location,
            shift=shift,
            components=components,
            leave_types=leave_types,
            policy=policy,
            employees=employees,
            structures=structures,
        )

    # ------------------------------------------------------------ internals
    @staticmethod
    async def _components(uow: UnitOfWork, company_id: UUID) -> dict[str, SalaryComponent]:
        """A deliberately mixed set: FIXED, PERCENTAGE and FORMULA, plus one
        of each component type, so the calculator is exercised end to end."""
        specs = [
            (
                "HOUSING",
                "Housing Allowance",
                SalaryComponentType.EARNING,
                CalculationType.PERCENTAGE,
                PercentageBase.BASE_SALARY,
                None,
                10,
                True,
                "6010",
            ),
            (
                "TRANSPORT",
                "Transport Allowance",
                SalaryComponentType.EARNING,
                CalculationType.FIXED,
                PercentageBase.BASE_SALARY,
                None,
                20,
                True,
                "6020",
            ),
            (
                "OVERTIME",
                "Overtime",
                SalaryComponentType.EARNING,
                CalculationType.FORMULA,
                PercentageBase.BASE_SALARY,
                "overtime_hourly_1_5x",
                30,
                True,
                "6030",
            ),
            (
                "PENSION",
                "Pension",
                SalaryComponentType.DEDUCTION,
                CalculationType.PERCENTAGE,
                PercentageBase.GROSS_EARNINGS,
                None,
                40,
                False,
                "2200",
            ),
            (
                "NI_ER",
                "Employer NI",
                SalaryComponentType.EMPLOYER_CONTRIBUTION,
                CalculationType.PERCENTAGE,
                PercentageBase.GROSS_EARNINGS,
                None,
                60,
                False,
                "6040",
            ),
        ]
        components: dict[str, SalaryComponent] = {}
        for code, name, kind, calc, base, formula, sequence, taxable, gl in specs:
            component = SalaryComponent(
                company_id=company_id,
                component_code=code,
                name=name,
                component_type=kind,
                calculation_type=calc,
                percentage_base=base,
                formula_key=formula,
                sequence=sequence,
                is_taxable=taxable,
                gl_account_code=gl,
                is_active=True,
            )
            uow.salary_components.add(component)
            components[code] = component
        await uow.flush()
        return components

    @staticmethod
    async def _leave_types(uow: UnitOfWork, company_id: UUID) -> dict[str, LeaveType]:
        specs = [
            ("ANNUAL", "Annual Leave", True, Decimal("25.00"), True),
            ("SICK", "Sick Leave", True, Decimal("10.00"), True),
            ("UNPAID", "Unpaid Leave", False, None, True),
            # Auto-approved: proves the no-approval-required path.
            ("COMP", "Compassionate Leave", True, Decimal("5.00"), False),
        ]
        leave_types: dict[str, LeaveType] = {}
        for code, name, is_paid, annual_limit, requires_approval in specs:
            leave_type = LeaveType(
                company_id=company_id,
                leave_code=code,
                name=name,
                is_paid=is_paid,
                annual_limit=annual_limit,
                requires_approval=requires_approval,
                excludes_non_working_days=True,
                allows_negative_balance=False,
                is_active=True,
            )
            uow.leave_types.add(leave_type)
            leave_types[code] = leave_type
        await uow.flush()
        return leave_types

    @staticmethod
    async def _policy(
        uow: UnitOfWork,
        session: AsyncSession,
        company_id: UUID,
        leave_types: dict[str, LeaveType],
    ) -> LeavePolicy:
        policy = LeavePolicy(company_id=company_id, name="Standard Policy", is_active=True)
        uow.leave_policies.add(policy)
        await uow.flush()
        for code, entitlement in [
            ("ANNUAL", Decimal("25.00")),
            ("SICK", Decimal("10.00")),
            ("COMP", Decimal("5.00")),
            ("UNPAID", Decimal("0.00")),
        ]:
            session.add(
                LeavePolicyRule(
                    policy_id=policy.id,
                    leave_type_id=leave_types[code].id,
                    annual_entitlement=entitlement,
                    carry_forward_allowed=False,
                    minimum_service_days=0,
                )
            )
        await uow.flush()
        return policy

    @staticmethod
    async def _grant_leave(
        uow: UnitOfWork,
        company_id: UUID,
        employee_id: UUID,
        leave_types: dict[str, LeaveType],
    ) -> None:
        """Grant entitlement the way the application does: transaction first,
        projection second — so a rebuild reproduces it exactly."""
        for code, days in [
            ("ANNUAL", Decimal("25.00")),
            ("SICK", Decimal("10.00")),
            ("COMP", Decimal("5.00")),
        ]:
            uow.leave_transactions.add(
                LeaveTransaction(
                    company_id=company_id,
                    employee_id=employee_id,
                    leave_type_id=leave_types[code].id,
                    year=YEAR,
                    transaction_type=LeaveTransactionType.ENTITLEMENT,
                    days=days,
                    source_type="POLICY_GRANT",
                )
            )
            await uow.leave_balances.apply_movement(
                company_id=company_id,
                employee_id=employee_id,
                leave_type_id=leave_types[code].id,
                year=YEAR,
                earned_delta=days,
            )

    @staticmethod
    async def _salary(
        uow: UnitOfWork,
        session: AsyncSession,
        company_id: UUID,
        employee_id: UUID,
        base_salary: Decimal,
        components: dict[str, SalaryComponent],
    ) -> EmployeeSalaryStructure:
        structure = EmployeeSalaryStructure(
            company_id=company_id,
            employee_id=employee_id,
            effective_from=HIRE_DATE,
            currency_code=CURRENCY,
            base_salary=base_salary,
            prorate_base_salary=True,
            status=SalaryStructureStatus.ACTIVE,
        )
        uow.salary_structures.add(structure)
        await uow.flush()

        for code, amount, percentage in [
            ("HOUSING", Decimal("0"), Decimal("15.0000")),
            ("TRANSPORT", Decimal("150.00"), None),
            ("OVERTIME", Decimal("0"), None),
            ("PENSION", Decimal("0"), Decimal("5.0000")),
            ("NI_ER", Decimal("0"), Decimal("13.8000")),
        ]:
            session.add(
                EmployeeSalaryComponent(
                    salary_structure_id=structure.id,
                    salary_component_id=components[code].id,
                    amount=amount,
                    percentage=percentage,
                )
            )
        await uow.flush()
        return structure


async def make_admin_token(client: object, session: AsyncSession) -> str:
    """Create a superuser and return a bearer token for it.

    Superuser rather than a role bundle: API tests are about routes and
    business rules, and re-testing the permission catalogue in every one of
    them would be noise. Permission enforcement has its own dedicated tests.
    """
    from app.core.security import hash_password
    from app.infrastructure.database.models.identity import User

    uow = UnitOfWork(session)
    user = User(
        email="apitest.admin@test.example",
        hashed_password=hash_password("test-password-1234"),
        full_name="API Test Admin",
        is_active=True,
        is_superuser=True,
    )
    uow.users.add(user)
    await uow.commit()

    response = await client.post(  # type: ignore[attr-defined]
        "/api/v1/auth/login",
        json={"email": user.email, "password": "test-password-1234"},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token
