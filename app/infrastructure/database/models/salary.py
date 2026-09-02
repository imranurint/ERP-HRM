"""Salary components and employee salary structures.

Every monetary column here is `NUMERIC(19,4)`. There is no float anywhere in
this file, and the pre-commit hook rejects one being introduced.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.payroll.enums import (
    CalculationType,
    PercentageBase,
    SalaryComponentType,
    SalaryStructureStatus,
)
from app.infrastructure.database.base import (
    ActorTrackingMixin,
    Base,
    PercentageNumeric,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    money_column,
)
from app.infrastructure.database.models.mixins import status_enum


class SalaryComponent(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """A named element of pay: an allowance, a deduction, an employer cost."""

    __tablename__ = "salary_components"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    component_code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    component_type: Mapped[SalaryComponentType] = mapped_column(
        status_enum(SalaryComponentType, "salary_component_type", 32), nullable=False
    )
    calculation_type: Mapped[CalculationType] = mapped_column(
        status_enum(CalculationType, "calculation_type", 16), nullable=False
    )
    percentage_base: Mapped[PercentageBase] = mapped_column(
        status_enum(PercentageBase, "percentage_base", 24),
        nullable=False,
        default=PercentageBase.BASE_SALARY,
    )
    #: Names a calculator in `app.domain.payroll.formulas`. Never an
    #: expression — see that module for why storing code in a column is not
    #: something this system does.
    formula_key: Mapped[str | None] = mapped_column(String(64))
    #: Evaluation order. Components are applied lowest-first and each sees the
    #: running totals of everything before it, which is what makes
    #: "5% of gross" unambiguous.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    is_taxable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: The GL account this component posts to when payroll reaches Accounting.
    #: A plain string, not a foreign key: the chart of accounts lives in
    #: another service's database and HRM must not depend on its schema.
    gl_account_code: Mapped[str | None] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint(
            "company_id", "component_code", name="uq_salary_components_company_id_component_code"
        ),
        CheckConstraint("length(trim(component_code)) > 0", name="component_code_not_blank"),
        CheckConstraint(
            "calculation_type <> 'FORMULA' OR formula_key IS NOT NULL",
            name="formula_requires_key",
        ),
        CheckConstraint("sequence >= 0", name="sequence_non_negative"),
        Index("ix_salary_components_company_active", "company_id", "is_active"),
        Index("ix_salary_components_company_type", "company_id", "component_type"),
    )


class EmployeeSalaryStructure(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """An employee's pay, effective over a date range.

    Structures for one employee **must not overlap**, enforced by a GiST
    exclusion constraint on `(employee_id, daterange(effective_from,
    coalesce(effective_to, 'infinity'), '[]'))`. Without it, "what was their
    salary on 2026-03-01?" could have two answers and payroll would silently
    pick whichever row came back first.
    """

    __tablename__ = "employee_salary_structures"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.currency_code", ondelete="RESTRICT"), nullable=False
    )
    base_salary: Mapped[Decimal] = money_column()
    #: Whether base salary is reduced by unworked days. Per-structure rather
    #: than global: a monthly-salaried manager and an hourly-paid operator are
    #: both employees, and only one of them should lose pay for a short day.
    prorate_base_salary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[SalaryStructureStatus] = mapped_column(
        status_enum(SalaryStructureStatus, "salary_structure_status", 16),
        nullable=False,
        default=SalaryStructureStatus.DRAFT,
    )
    notes: Mapped[str | None] = mapped_column(Text)

    components: Mapped[list[EmployeeSalaryComponent]] = relationship(
        back_populates="salary_structure",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="structure_date_order"
        ),
        CheckConstraint("base_salary >= 0", name="base_salary_non_negative"),
        Index("ix_employee_salary_structures_employee", "employee_id", "effective_from"),
        Index("ix_employee_salary_structures_company_status", "company_id", "status"),
    )


class EmployeeSalaryComponent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One component attached to one employee's salary structure."""

    __tablename__ = "employee_salary_components"

    salary_structure_id: Mapped[UUID] = mapped_column(
        ForeignKey("employee_salary_structures.id", ondelete="CASCADE"), nullable=False
    )
    salary_component_id: Mapped[UUID] = mapped_column(
        ForeignKey("salary_components.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = money_column()
    percentage: Mapped[Decimal | None] = mapped_column(PercentageNumeric)
    #: Overrides the component's own sequence for this employee, when a
    #: particular contract needs a different order of operations.
    sequence_override: Mapped[int | None] = mapped_column(Integer)

    salary_structure: Mapped[EmployeeSalaryStructure] = relationship(back_populates="components")
    component: Mapped[SalaryComponent] = relationship(lazy="joined")

    __table_args__ = (
        # Shortened by hand: the naming convention would generate a name past
        # PostgreSQL's 63-character identifier limit.
        UniqueConstraint(
            "salary_structure_id",
            "salary_component_id",
            name="uq_employee_salary_components_structure_component",
        ),
        CheckConstraint("amount >= 0", name="component_amount_non_negative"),
        CheckConstraint(
            "percentage IS NULL OR (percentage >= 0 AND percentage <= 100)",
            name="component_percentage_range",
        ),
        Index("ix_employee_salary_components_structure", "salary_structure_id"),
    )


__all__ = ["EmployeeSalaryComponent", "EmployeeSalaryStructure", "SalaryComponent"]
