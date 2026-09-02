"""Salary component and salary structure service.

Activating a salary structure is a **salary change**, so it writes a lifecycle
event and a `SalaryChanged` domain event alongside the row. Pay is the thing
people query most often and dispute most bitterly; every change to it leaves a
trail.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from app.application.context import ActorContext
from app.application.salary.schemas import (
    FormulaRead,
    SalaryComponentCreate,
    SalaryComponentRead,
    SalaryComponentUpdate,
    SalaryStructureComponentRead,
    SalaryStructureCreate,
    SalaryStructureRead,
)
from app.core.exceptions import DuplicateResourceError, NotFoundError
from app.core.pagination import Page, PageParams
from app.domain.employees.enums import LifecycleEventType
from app.domain.employees.exceptions import EmployeeNotFoundError
from app.domain.payroll.enums import SalaryStructureStatus
from app.domain.payroll.exceptions import (
    SalaryComponentNotFoundError,
    SalaryStructureInvalidError,
    SalaryStructureNotFoundError,
)
from app.domain.payroll.formulas import registered_formulas
from app.domain.payroll.rules import (
    ensure_base_salary,
    ensure_component_configuration,
    ensure_no_structure_overlap,
    ensure_structure_dates,
)
from app.domain.shared.enums import AuditAction
from app.domain.shared.events import SalaryChanged
from app.infrastructure.database.models.employee import Employee, EmployeeLifecycleEvent
from app.infrastructure.database.models.salary import (
    EmployeeSalaryComponent,
    EmployeeSalaryStructure,
    SalaryComponent,
)
from app.infrastructure.database.unit_of_work import UnitOfWork


class SalaryService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    # ------------------------------------------------------------ components
    async def create_component(
        self, company_id: UUID, payload: SalaryComponentCreate, actor: ActorContext
    ) -> SalaryComponentRead:
        if await self.uow.salary_components.get_by_code(company_id, payload.component_code):
            raise DuplicateResourceError(
                "That salary component code already exists for this company.",
                details={"component_code": payload.component_code},
            )

        # Validated at configuration time so a bad component cannot blow up
        # mid-payroll-run, when half the payslips already exist.
        ensure_component_configuration(
            component_code=payload.component_code,
            calculation_type=payload.calculation_type,
            component_type=payload.component_type,
            amount=Decimal("0"),
            percentage=Decimal("0") if payload.calculation_type.name == "PERCENTAGE" else None,
            formula_key=payload.formula_key,
        )

        component = SalaryComponent(
            company_id=company_id,
            component_code=payload.component_code,
            name=payload.name,
            description=payload.description,
            component_type=payload.component_type,
            calculation_type=payload.calculation_type,
            percentage_base=payload.percentage_base,
            formula_key=payload.formula_key,
            sequence=payload.sequence,
            is_taxable=payload.is_taxable,
            gl_account_code=payload.gl_account_code,
            is_active=True,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.salary_components.add(component)
        await self.uow.flush()

        self.uow.audit.record(
            action=AuditAction.SALARY_COMPONENT_CREATED,
            entity_type="SalaryComponent",
            entity_id=component.id,
            company_id=company_id,
            user_id=actor.user_id,
            new_data={
                "component_code": component.component_code,
                "component_type": str(component.component_type),
                "calculation_type": str(component.calculation_type),
                "gl_account_code": component.gl_account_code,
            },
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()
        await self.uow.refresh(component)
        return SalaryComponentRead.model_validate(component)

    async def update_component(
        self,
        company_id: UUID,
        component_id: UUID,
        payload: SalaryComponentUpdate,
        actor: ActorContext,
    ) -> SalaryComponentRead:
        component = await self._require_component(company_id, component_id)
        changes = payload.model_dump(exclude_unset=True)
        previous = {field: getattr(component, field) for field in changes}
        for field, value in changes.items():
            setattr(component, field, value)
        component.updated_by = actor.user_id

        self.uow.audit.record(
            action=AuditAction.SALARY_COMPONENT_UPDATED,
            entity_type="SalaryComponent",
            entity_id=component.id,
            company_id=company_id,
            user_id=actor.user_id,
            previous_data=previous,
            new_data=changes,
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()
        return SalaryComponentRead.model_validate(component)

    async def list_components(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> Page[SalaryComponentRead]:
        rows, total = await self.uow.salary_components.list_for_company(
            company_id, params, is_active=is_active
        )
        return Page.build(
            [SalaryComponentRead.model_validate(r) for r in rows], params, total=total
        )

    @staticmethod
    def list_formulas() -> list[FormulaRead]:
        """The registered formula catalogue.

        Surfaced so an administrator configuring a FORMULA component can pick
        a valid key instead of guessing and discovering the mistake at
        payroll time.
        """
        return [
            FormulaRead(formula_key=key, description=description)
            for key, description in sorted(registered_formulas().items())
        ]

    # ------------------------------------------------------------ structures
    async def create_structure(
        self, company_id: UUID, payload: SalaryStructureCreate, actor: ActorContext
    ) -> SalaryStructureRead:
        employee = await self._require_employee(company_id, payload.employee_id)

        ensure_structure_dates(payload.effective_from, payload.effective_to)
        ensure_base_salary(payload.base_salary)

        currency = await self.uow.currencies.get_by_code(payload.currency_code)
        if currency is None:
            raise NotFoundError(
                "Currency not found.", details={"currency_code": payload.currency_code}
            )

        if payload.activate:
            # Also enforced by a GiST exclusion constraint — this check just
            # produces a better message when there is no race.
            existing = await self.uow.salary_structures.existing_ranges(employee.id)
            ensure_no_structure_overlap(payload.effective_from, payload.effective_to, existing)

        structure = EmployeeSalaryStructure(
            company_id=company_id,
            employee_id=employee.id,
            effective_from=payload.effective_from,
            effective_to=payload.effective_to,
            currency_code=currency.currency_code,
            base_salary=payload.base_salary,
            prorate_base_salary=payload.prorate_base_salary,
            status=(
                SalaryStructureStatus.ACTIVE if payload.activate else SalaryStructureStatus.DRAFT
            ),
            notes=payload.notes,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.salary_structures.add(structure)
        await self.uow.flush()

        for line in payload.components:
            component = await self._require_component(company_id, line.salary_component_id)
            ensure_component_configuration(
                component_code=component.component_code,
                calculation_type=component.calculation_type,
                component_type=component.component_type,
                amount=line.amount,
                percentage=line.percentage,
                formula_key=component.formula_key,
            )
            self.uow.session.add(
                EmployeeSalaryComponent(
                    salary_structure_id=structure.id,
                    salary_component_id=component.id,
                    amount=line.amount,
                    percentage=line.percentage,
                    sequence_override=line.sequence_override,
                )
            )
        await self.uow.flush()

        previous_salary = await self._previous_base_salary(employee.id, structure.id)
        self.uow.audit.record(
            action=(
                AuditAction.SALARY_STRUCTURE_ACTIVATED
                if payload.activate
                else AuditAction.SALARY_STRUCTURE_CREATED
            ),
            entity_type="EmployeeSalaryStructure",
            entity_id=structure.id,
            company_id=company_id,
            user_id=actor.user_id,
            previous_data=(
                {"base_salary": str(previous_salary)} if previous_salary is not None else None
            ),
            new_data={
                "base_salary": str(structure.base_salary),
                "currency_code": structure.currency_code,
                "effective_from": structure.effective_from.isoformat(),
                "status": str(structure.status),
            },
            context={"employee_id": str(employee.id)},
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )

        if payload.activate:
            self._record_salary_change(structure, employee, previous_salary, actor)

        await self.uow.commit()
        loaded = await self.uow.salary_structures.get_scoped(company_id, structure.id)
        assert loaded is not None
        return self._to_read(loaded)

    async def activate_structure(
        self, company_id: UUID, structure_id: UUID, actor: ActorContext
    ) -> SalaryStructureRead:
        structure = await self._require_structure(company_id, structure_id)
        if structure.status is SalaryStructureStatus.EXPIRED:
            raise SalaryStructureInvalidError(
                "An expired salary structure cannot be activated.",
                details={"status": str(structure.status)},
            )
        if structure.status is SalaryStructureStatus.ACTIVE:
            return self._to_read(structure)

        employee = await self._require_employee(company_id, structure.employee_id)
        existing = await self.uow.salary_structures.existing_ranges(
            employee.id, exclude_id=structure.id
        )
        ensure_no_structure_overlap(structure.effective_from, structure.effective_to, existing)

        previous_salary = await self._previous_base_salary(employee.id, structure.id)
        structure.status = SalaryStructureStatus.ACTIVE
        structure.updated_by = actor.user_id

        self.uow.audit.record(
            action=AuditAction.SALARY_STRUCTURE_ACTIVATED,
            entity_type="EmployeeSalaryStructure",
            entity_id=structure.id,
            company_id=company_id,
            user_id=actor.user_id,
            previous_data=(
                {"base_salary": str(previous_salary)} if previous_salary is not None else None
            ),
            new_data={"base_salary": str(structure.base_salary), "status": "ACTIVE"},
            context={"employee_id": str(employee.id)},
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        self._record_salary_change(structure, employee, previous_salary, actor)
        await self.uow.commit()
        return self._to_read(structure)

    async def get_structure(self, company_id: UUID, structure_id: UUID) -> SalaryStructureRead:
        return self._to_read(await self._require_structure(company_id, structure_id))

    async def list_structures_for_employee(
        self, company_id: UUID, employee_id: UUID
    ) -> list[SalaryStructureRead]:
        await self._require_employee(company_id, employee_id)
        rows = await self.uow.salary_structures.list_for_employee(employee_id)
        return [self._to_read(row) for row in rows]

    async def current_structure(
        self, company_id: UUID, employee_id: UUID, as_of: object
    ) -> SalaryStructureRead:
        from datetime import date as _date

        await self._require_employee(company_id, employee_id)
        assert isinstance(as_of, _date)
        row = await self.uow.salary_structures.effective_on(employee_id, as_of)
        if row is None:
            raise SalaryStructureNotFoundError(
                details={"employee_id": str(employee_id), "as_of": as_of.isoformat()}
            )
        return self._to_read(row)

    # ------------------------------------------------------------- internals
    def _record_salary_change(
        self,
        structure: EmployeeSalaryStructure,
        employee: Employee,
        previous_salary: Decimal | None,
        actor: ActorContext,
    ) -> None:
        self.uow.lifecycle.add(
            EmployeeLifecycleEvent(
                company_id=structure.company_id,
                employee_id=employee.id,
                event_type=LifecycleEventType.SALARY_CHANGED,
                effective_date=structure.effective_from,
                previous_data=(
                    {"base_salary": str(previous_salary)} if previous_salary is not None else {}
                ),
                new_data={
                    "base_salary": str(structure.base_salary),
                    "currency_code": structure.currency_code,
                },
                created_by=actor.user_id,
            )
        )
        self.uow.outbox.enqueue(
            SalaryChanged(
                aggregate_id=structure.id,
                company_id=structure.company_id,
                employee_id=employee.id,
                employee_number=employee.employee_number,
                previous_base_salary=previous_salary,
                new_base_salary=structure.base_salary,
                currency_code=structure.currency_code,
                effective_from=structure.effective_from,
                changed_by=actor.user_id,
            )
        )

    async def _previous_base_salary(self, employee_id: UUID, exclude_id: UUID) -> Decimal | None:
        rows = await self.uow.salary_structures.list_for_employee(employee_id)
        for row in rows:
            if row.id != exclude_id and row.status is not SalaryStructureStatus.DRAFT:
                return Decimal(str(row.base_salary))
        return None

    async def _require_employee(self, company_id: UUID, employee_id: UUID) -> Employee:
        row = await self.uow.employees.get_scoped(company_id, employee_id)
        if row is None:
            raise EmployeeNotFoundError(details={"employee_id": str(employee_id)})
        return row

    async def _require_component(self, company_id: UUID, component_id: UUID) -> SalaryComponent:
        row = await self.uow.salary_components.get_scoped(company_id, component_id)
        if row is None:
            raise SalaryComponentNotFoundError(details={"component_id": str(component_id)})
        return row

    async def _require_structure(
        self, company_id: UUID, structure_id: UUID
    ) -> EmployeeSalaryStructure:
        row = await self.uow.salary_structures.get_scoped(company_id, structure_id)
        if row is None:
            raise SalaryStructureNotFoundError(details={"structure_id": str(structure_id)})
        return row

    @staticmethod
    def _to_read(structure: EmployeeSalaryStructure) -> SalaryStructureRead:
        components = [
            SalaryStructureComponentRead(
                id=line.id,
                salary_component_id=line.salary_component_id,
                component_code=line.component.component_code,
                component_name=line.component.name,
                component_type=line.component.component_type,
                calculation_type=line.component.calculation_type,
                amount=Decimal(str(line.amount)),
                percentage=(Decimal(str(line.percentage)) if line.percentage is not None else None),
                sequence=(
                    line.sequence_override
                    if line.sequence_override is not None
                    else line.component.sequence
                ),
            )
            for line in structure.components
        ]
        components.sort(key=lambda c: (c.sequence, c.component_code))
        # Built field by field rather than `model_validate(structure)`: the
        # component read model flattens attributes that live on the joined
        # SalaryComponent, so validating the ORM rows directly would fail.
        return SalaryStructureRead(
            id=structure.id,
            company_id=structure.company_id,
            employee_id=structure.employee_id,
            effective_from=structure.effective_from,
            effective_to=structure.effective_to,
            currency_code=structure.currency_code,
            base_salary=Decimal(str(structure.base_salary)),
            prorate_base_salary=structure.prorate_base_salary,
            status=structure.status,
            notes=structure.notes,
            components=components,
            created_at=structure.created_at,
            updated_at=structure.updated_at,
        )
