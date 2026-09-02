"""Employment contract service."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.application.context import ActorContext
from app.application.contracts.schemas import (
    ContractCreate,
    ContractRead,
    ContractTerminateRequest,
    ContractUpdate,
)
from app.core.dates import business_today
from app.domain.employees.enums import ContractStatus, LifecycleEventType
from app.domain.employees.exceptions import (
    ActiveContractExistsError,
    ContractNotFoundError,
    EmployeeNotFoundError,
    InvalidContractError,
)
from app.domain.employees.rules import (
    contract_status_on,
    ensure_contract_dates,
    ensure_contract_mutable,
    ensure_employed,
    ensure_working_hours,
)
from app.domain.shared.enums import AuditAction
from app.infrastructure.database.models.employee import (
    Employee,
    EmployeeLifecycleEvent,
    EmploymentContract,
)
from app.infrastructure.database.unit_of_work import UnitOfWork


class ContractService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def create(
        self, company_id: UUID, payload: ContractCreate, actor: ActorContext
    ) -> ContractRead:
        employee = await self._require_employee(company_id, payload.employee_id)
        ensure_employed(employee.employment_status, action="issue a contract for this employee")

        ensure_contract_dates(
            contract_type=payload.contract_type,
            start_date=payload.start_date,
            end_date=payload.end_date,
            probation_end_date=payload.probation_end_date,
        )
        ensure_working_hours(payload.working_hours_per_week)

        if payload.activate and await self.uow.contracts.get_active(employee.id) is not None:
            raise ActiveContractExistsError(details={"employee_id": str(employee.id)})

        contract_number = payload.contract_number
        if contract_number:
            contract_number = contract_number.strip().upper()
        else:
            contract_number = await self.uow.sequences.allocate_contract_number(
                company_id, payload.start_date.year
            )

        contract = EmploymentContract(
            company_id=company_id,
            employee_id=employee.id,
            contract_number=contract_number,
            contract_type=payload.contract_type,
            start_date=payload.start_date,
            end_date=payload.end_date,
            probation_end_date=payload.probation_end_date,
            working_hours_per_week=payload.working_hours_per_week,
            status=ContractStatus.ACTIVE if payload.activate else ContractStatus.DRAFT,
            notes=payload.notes,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.contracts.add(contract)
        await self.uow.flush()

        self._audit(
            AuditAction.CONTRACT_CREATED,
            contract,
            actor,
            new_data=_snapshot(contract),
        )
        if payload.activate:
            self._lifecycle(contract, employee, actor, new_data=_snapshot(contract))
        await self.uow.commit()
        await self.uow.refresh(contract)
        return ContractRead.model_validate(contract)

    async def activate(
        self, company_id: UUID, contract_id: UUID, actor: ActorContext
    ) -> ContractRead:
        """Put a DRAFT contract into force.

        The partial unique index is the real guarantee here: two operators
        activating two different draft contracts at the same moment would both
        pass this check, and exactly one of their transactions will commit.
        """
        contract = await self._require_contract(company_id, contract_id)
        if contract.status is ContractStatus.ACTIVE:
            raise ActiveContractExistsError(
                "This contract is already active.", details={"contract_id": str(contract_id)}
            )
        ensure_contract_mutable(contract.status)

        employee = await self._require_employee(company_id, contract.employee_id)
        if await self.uow.contracts.get_active(employee.id) is not None:
            raise ActiveContractExistsError(details={"employee_id": str(employee.id)})

        previous = {"status": str(contract.status)}
        contract.status = ContractStatus.ACTIVE
        contract.updated_by = actor.user_id

        self._audit(
            AuditAction.CONTRACT_ACTIVATED,
            contract,
            actor,
            previous_data=previous,
            new_data={"status": str(contract.status)},
        )
        self._lifecycle(contract, employee, actor, new_data=_snapshot(contract))
        await self.uow.commit()
        return ContractRead.model_validate(contract)

    async def update(
        self, company_id: UUID, contract_id: UUID, payload: ContractUpdate, actor: ActorContext
    ) -> ContractRead:
        contract = await self._require_contract(company_id, contract_id)
        ensure_contract_mutable(contract.status)

        changes = payload.model_dump(exclude_unset=True)
        merged_end = changes.get("end_date", contract.end_date)
        merged_probation = changes.get("probation_end_date", contract.probation_end_date)
        ensure_contract_dates(
            contract_type=contract.contract_type,
            start_date=contract.start_date,
            end_date=merged_end,
            probation_end_date=merged_probation,
        )
        if "working_hours_per_week" in changes:
            ensure_working_hours(changes["working_hours_per_week"])

        previous = {field: _jsonable(getattr(contract, field)) for field in changes}
        for field, value in changes.items():
            setattr(contract, field, value)
        contract.updated_by = actor.user_id

        self._audit(
            AuditAction.CONTRACT_CREATED,
            contract,
            actor,
            previous_data=previous,
            new_data={k: _jsonable(v) for k, v in changes.items()},
        )
        await self.uow.commit()
        return ContractRead.model_validate(contract)

    async def terminate(
        self,
        company_id: UUID,
        contract_id: UUID,
        payload: ContractTerminateRequest,
        actor: ActorContext,
    ) -> ContractRead:
        contract = await self._require_contract(company_id, contract_id)
        ensure_contract_mutable(contract.status)
        if payload.end_date < contract.start_date:
            raise InvalidContractError(
                "A contract cannot end before it starts.",
                details={
                    "start_date": contract.start_date.isoformat(),
                    "end_date": payload.end_date.isoformat(),
                },
            )

        previous = {"status": str(contract.status), "end_date": _jsonable(contract.end_date)}
        contract.status = ContractStatus.TERMINATED
        contract.end_date = payload.end_date
        contract.updated_by = actor.user_id

        self._audit(
            AuditAction.CONTRACT_TERMINATED,
            contract,
            actor,
            previous_data=previous,
            new_data={
                "status": str(contract.status),
                "end_date": _jsonable(contract.end_date),
                "reason": payload.reason,
            },
        )
        await self.uow.commit()
        return ContractRead.model_validate(contract)

    async def get(self, company_id: UUID, contract_id: UUID) -> ContractRead:
        contract = await self._require_contract(company_id, contract_id)
        return self._with_derived_status(company_id, contract)

    async def list_for_employee(self, company_id: UUID, employee_id: UUID) -> list[ContractRead]:
        await self._require_employee(company_id, employee_id)
        rows = await self.uow.contracts.list_for_employee(employee_id)
        return [self._with_derived_status(company_id, row) for row in rows]

    # ------------------------------------------------------------- internals
    def _with_derived_status(self, company_id: UUID, contract: EmploymentContract) -> ContractRead:
        """Report expiry as of today even if no batch job has run.

        Expiry is derived rather than driven by a scheduled task, so payroll
        can never pay against a lapsed contract because a cron job was down.
        """
        read = ContractRead.model_validate(contract)
        return read.model_copy(
            update={
                "status": contract_status_on(
                    status=contract.status,
                    end_date=contract.end_date,
                    as_of=business_today(),
                )
            }
        )

    async def _require_employee(self, company_id: UUID, employee_id: UUID) -> Employee:
        row = await self.uow.employees.get_scoped(company_id, employee_id)
        if row is None:
            raise EmployeeNotFoundError(details={"employee_id": str(employee_id)})
        return row

    async def _require_contract(self, company_id: UUID, contract_id: UUID) -> EmploymentContract:
        row = await self.uow.contracts.get_scoped(company_id, contract_id)
        if row is None:
            raise ContractNotFoundError(details={"contract_id": str(contract_id)})
        return row

    def _lifecycle(
        self,
        contract: EmploymentContract,
        employee: Employee,
        actor: ActorContext,
        *,
        new_data: dict[str, Any],
    ) -> None:
        self.uow.lifecycle.add(
            EmployeeLifecycleEvent(
                company_id=contract.company_id,
                employee_id=employee.id,
                event_type=LifecycleEventType.CONTRACT_CHANGED,
                effective_date=contract.start_date,
                previous_data={},
                new_data=new_data,
                created_by=actor.user_id,
            )
        )

    def _audit(
        self,
        action: AuditAction,
        contract: EmploymentContract,
        actor: ActorContext,
        *,
        previous_data: dict[str, Any] | None = None,
        new_data: dict[str, Any] | None = None,
    ) -> None:
        self.uow.audit.record(
            action=action,
            entity_type="EmploymentContract",
            entity_id=contract.id,
            company_id=contract.company_id,
            user_id=actor.user_id,
            previous_data=previous_data,
            new_data=new_data,
            context={
                "contract_number": contract.contract_number,
                "employee_id": str(contract.employee_id),
            },
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )


def _snapshot(contract: EmploymentContract) -> dict[str, Any]:
    return {
        "contract_number": contract.contract_number,
        "contract_type": str(contract.contract_type),
        "start_date": _jsonable(contract.start_date),
        "end_date": _jsonable(contract.end_date),
        "probation_end_date": _jsonable(contract.probation_end_date),
        "working_hours_per_week": contract.working_hours_per_week,
        "status": str(contract.status),
    }


def _jsonable(value: object) -> object:
    from datetime import date as _date
    from datetime import datetime as _dt

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, _dt | _date):
        return value.isoformat()
    if hasattr(value, "value"):
        return str(value)
    return value


__all__ = ["ContractService"]
