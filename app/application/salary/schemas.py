"""Salary component and salary structure schemas."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field, model_validator

from app.application.common import AppModel, CodeStr, CurrencyCode, MoneyStr, ORMModel, RateStr
from app.domain.payroll.enums import (
    CalculationType,
    PercentageBase,
    SalaryComponentType,
    SalaryStructureStatus,
)


# ------------------------------------------------------------------ components
class SalaryComponentCreate(AppModel):
    component_code: CodeStr = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    component_type: SalaryComponentType
    calculation_type: CalculationType
    description: str | None = None
    percentage_base: PercentageBase = PercentageBase.BASE_SALARY
    formula_key: str | None = Field(
        default=None,
        max_length=64,
        description="Names a registered calculator. Required for FORMULA components.",
    )
    sequence: int = Field(
        default=100,
        ge=0,
        description="Evaluation order. Each component sees the running totals "
        "of everything with a lower sequence.",
    )
    is_taxable: bool = True
    gl_account_code: str | None = Field(
        default=None,
        max_length=32,
        description="GL account this component posts to when payroll reaches Accounting.",
    )


class SalaryComponentUpdate(AppModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    sequence: int | None = Field(default=None, ge=0)
    is_taxable: bool | None = None
    gl_account_code: str | None = Field(default=None, max_length=32)
    is_active: bool | None = None


class SalaryComponentRead(ORMModel):
    id: UUID
    company_id: UUID
    component_code: str
    name: str
    description: str | None
    component_type: SalaryComponentType
    calculation_type: CalculationType
    percentage_base: PercentageBase
    formula_key: str | None
    sequence: int
    is_taxable: bool
    gl_account_code: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class FormulaRead(AppModel):
    """A registered payroll formula, surfaced so HR can pick from a list."""

    formula_key: str
    description: str


# ------------------------------------------------------------------ structures
class StructureComponentInput(AppModel):
    salary_component_id: UUID
    amount: MoneyStr = Field(default=Decimal("0"))
    percentage: RateStr | None = None
    sequence_override: int | None = Field(default=None, ge=0)


class SalaryStructureCreate(AppModel):
    employee_id: UUID
    effective_from: date
    base_salary: MoneyStr
    currency_code: CurrencyCode = Field(min_length=3, max_length=3)
    effective_to: date | None = None
    prorate_base_salary: bool = True
    components: list[StructureComponentInput] = Field(default_factory=list)
    notes: str | None = None
    #: Activate immediately. The exclusion constraint rejects an overlap.
    activate: bool = False

    @model_validator(mode="after")
    def _no_duplicate_components(self) -> SalaryStructureCreate:
        ids = [c.salary_component_id for c in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError("A component may appear at most once in a salary structure.")
        return self


class SalaryStructureComponentRead(ORMModel):
    id: UUID
    salary_component_id: UUID
    component_code: str
    component_name: str
    component_type: SalaryComponentType
    calculation_type: CalculationType
    amount: MoneyStr
    percentage: RateStr | None
    sequence: int


class SalaryStructureRead(ORMModel):
    id: UUID
    company_id: UUID
    employee_id: UUID
    effective_from: date
    effective_to: date | None
    currency_code: str
    base_salary: MoneyStr
    prorate_base_salary: bool
    status: SalaryStructureStatus
    notes: str | None
    components: list[SalaryStructureComponentRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
