"""Company application service."""

from __future__ import annotations

from uuid import UUID

from app.application.common import isoformat
from app.application.companies.schemas import (
    CompanyCreate,
    CompanyRead,
    CompanyUpdate,
    CurrencyRead,
)
from app.application.context import ActorContext
from app.core.dates import resolve_timezone
from app.core.exceptions import NotFoundError, ValidationError
from app.core.pagination import Page, PageParams
from app.domain.shared.enums import AuditAction
from app.infrastructure.database.models.organization import Company
from app.infrastructure.database.unit_of_work import UnitOfWork


class CompanyService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def create(self, payload: CompanyCreate, actor: ActorContext) -> CompanyRead:
        currency = await self.uow.currencies.get_by_code(payload.base_currency_code)
        if currency is None:
            raise NotFoundError(
                "Base currency not found.",
                details={"currency_code": payload.base_currency_code},
            )
        if not currency.is_active:
            raise ValidationError(
                "That currency is not active.",
                code="CURRENCY_INACTIVE",
                details={"currency_code": payload.base_currency_code},
            )
        _validate_timezone(payload.timezone)

        company = Company(
            name=payload.name,
            legal_name=payload.legal_name,
            registration_number=payload.registration_number,
            email=str(payload.email) if payload.email else None,
            phone=payload.phone,
            address=payload.address,
            timezone=payload.timezone,
            base_currency_code=currency.currency_code,
            is_active=True,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.companies.add(company)
        await self.uow.flush()

        self.uow.audit.record(
            action=AuditAction.COMPANY_CREATED,
            entity_type="Company",
            entity_id=company.id,
            company_id=company.id,
            user_id=actor.user_id,
            new_data={
                "name": company.name,
                "legal_name": company.legal_name,
                "base_currency_code": company.base_currency_code,
                "timezone": company.timezone,
            },
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()
        return CompanyRead.model_validate(company)

    async def get(self, company_id: UUID) -> CompanyRead:
        company = await self._require(company_id)
        return CompanyRead.model_validate(company)

    async def list_page(
        self, params: PageParams, *, is_active: bool | None = None
    ) -> Page[CompanyRead]:
        rows, total = await self.uow.companies.list_paginated(params, is_active=is_active)
        return Page.build([CompanyRead.model_validate(row) for row in rows], params, total=total)

    async def update(
        self, company_id: UUID, payload: CompanyUpdate, actor: ActorContext
    ) -> CompanyRead:
        company = await self._require(company_id)
        changes = payload.model_dump(exclude_none=True)
        if "timezone" in changes:
            _validate_timezone(changes["timezone"])
        if "email" in changes and changes["email"] is not None:
            changes["email"] = str(changes["email"])

        previous = {field: _serialise(getattr(company, field)) for field in changes}
        for field, value in changes.items():
            setattr(company, field, value)
        company.updated_by = actor.user_id

        self.uow.audit.record(
            action=AuditAction.COMPANY_UPDATED,
            entity_type="Company",
            entity_id=company.id,
            company_id=company.id,
            user_id=actor.user_id,
            previous_data=previous,
            new_data={k: _serialise(v) for k, v in changes.items()},
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()
        return CompanyRead.model_validate(company)

    async def list_currencies(self) -> list[CurrencyRead]:
        rows = await self.uow.currencies.list_active()
        return [CurrencyRead.model_validate(row) for row in rows]

    async def _require(self, company_id: UUID) -> Company:
        company = await self.uow.companies.get(company_id)
        if company is None:
            raise NotFoundError("Company not found.", details={"company_id": str(company_id)})
        return company


def _validate_timezone(name: str) -> None:
    """Reject an unknown IANA zone at write time.

    `resolve_timezone` degrades to UTC at read time so attendance capture
    never fails, but storing a typo would silently file a whole office's
    attendance on the wrong day. Catch it here instead.
    """
    if str(resolve_timezone(name)) != name and name != "UTC":
        raise ValidationError(
            f"Unknown timezone {name!r}. Use an IANA name such as 'Europe/London'.",
            details={"timezone": name},
        )


def _serialise(value: object) -> object:
    from datetime import date, datetime

    if isinstance(value, datetime | date):
        return isoformat(value)
    return value
