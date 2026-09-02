"""Employees, employment contracts and lifecycle history.

Employees are **never hard-deleted**. Departure is an `employment_status`, so
every attendance record, leave transaction and payslip the person ever
generated stays joinable and reportable. That is a legal requirement in most
jurisdictions and an operational one everywhere.
"""

from __future__ import annotations

from datetime import date
from typing import Any
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.employees.enums import (
    ContractStatus,
    ContractType,
    EmploymentStatus,
    Gender,
    LifecycleEventType,
)
from app.infrastructure.database.base import (
    ActorTrackingMixin,
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from app.infrastructure.database.models.mixins import CreatedAtMixin, status_enum


class Employee(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    __tablename__ = "employees"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    employee_number: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Optional link to a login. Most employees in a deployment never get one;
    #: those who do gain self-service access scoped to this row.
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    work_email: Mapped[str] = mapped_column(String(320), nullable=False)
    personal_email: Mapped[str | None] = mapped_column(String(320))
    work_phone: Mapped[str | None] = mapped_column(String(50))
    personal_phone: Mapped[str | None] = mapped_column(String(50))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[Gender | None] = mapped_column(status_enum(Gender, "gender", 16))
    nationality: Mapped[str | None] = mapped_column(String(100))

    hire_date: Mapped[date] = mapped_column(Date, nullable=False)
    termination_date: Mapped[date | None] = mapped_column(Date)
    employment_status: Mapped[EmploymentStatus] = mapped_column(
        status_enum(EmploymentStatus, "employment_status"),
        nullable=False,
        default=EmploymentStatus.PROBATION,
    )

    department_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT")
    )
    designation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("designations.id", ondelete="RESTRICT")
    )
    manager_id: Mapped[UUID | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    location_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT")
    )

    #: Denormalised convenience flag kept in step with `employment_status` by
    #: the lifecycle service and guarded by a CHECK so the two cannot diverge.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Optimistic lock. Two concurrent PATCHes to the same employee must not
    #: silently overwrite each other's fields.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    manager: Mapped[Employee | None] = relationship(remote_side="Employee.id", lazy="noload")

    __table_args__ = (
        UniqueConstraint(
            "company_id", "employee_number", name="uq_employees_company_id_employee_number"
        ),
        UniqueConstraint("company_id", "work_email", name="uq_employees_company_id_work_email"),
        CheckConstraint("id <> manager_id", name="not_own_manager"),
        CheckConstraint(
            "termination_date IS NULL OR termination_date >= hire_date",
            name="termination_after_hire",
        ),
        CheckConstraint(
            "date_of_birth IS NULL OR date_of_birth < hire_date",
            name="born_before_hire",
        ),
        # is_active is a projection of employment_status; this is what stops
        # the two drifting apart under a partial update.
        CheckConstraint(
            "(is_active = true AND employment_status IN "
            "('ACTIVE', 'PROBATION', 'ON_LEAVE', 'SUSPENDED'))"
            " OR (is_active = false AND employment_status IN "
            "('TERMINATED', 'RESIGNED', 'INACTIVE'))",
            name="is_active_matches_status",
        ),
        # A terminal status without a termination date leaves "when did they
        # leave?" unanswerable, which breaks tenure and final-pay reporting.
        CheckConstraint(
            "employment_status NOT IN ('TERMINATED', 'RESIGNED') OR termination_date IS NOT NULL",
            name="terminal_status_has_date",
        ),
        CheckConstraint("length(trim(employee_number)) > 0", name="employee_number_not_blank"),
        Index("ix_employees_company_status", "company_id", "employment_status"),
        Index("ix_employees_company_department", "company_id", "department_id"),
        Index("ix_employees_company_designation", "company_id", "designation_id"),
        Index("ix_employees_manager", "manager_id"),
        Index(
            "ix_employees_user",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index("ix_employees_hire_date", "company_id", "hire_date"),
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class EmploymentContract(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """An employment contract.

    **At most one ACTIVE contract per employee**, enforced by a partial unique
    index (`uq_employment_contracts_one_active`) rather than an application
    check — two concurrent activations would both pass an application check
    and leave the employee with two contradictory contracts, and payroll would
    then have to guess which one to pay against.
    """

    __tablename__ = "employment_contracts"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    contract_number: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_type: Mapped[ContractType] = mapped_column(
        status_enum(ContractType, "contract_type"), nullable=False
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    probation_end_date: Mapped[date | None] = mapped_column(Date)
    working_hours_per_week: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[ContractStatus] = mapped_column(
        status_enum(ContractStatus, "contract_status"),
        nullable=False,
        default=ContractStatus.DRAFT,
    )
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "contract_number",
            name="uq_employment_contracts_company_id_contract_number",
        ),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="contract_date_order"),
        CheckConstraint(
            "probation_end_date IS NULL OR probation_end_date >= start_date",
            name="probation_after_start",
        ),
        CheckConstraint(
            "working_hours_per_week IS NULL OR working_hours_per_week BETWEEN 1 AND 168",
            name="working_hours_range",
        ),
        # A fixed-term contract with no end date can never expire.
        CheckConstraint(
            "contract_type = 'PERMANENT' OR end_date IS NOT NULL",
            name="fixed_term_has_end_date",
        ),
        Index("ix_employment_contracts_employee", "employee_id", "status"),
        Index("ix_employment_contracts_company_status", "company_id", "status"),
        Index("ix_employment_contracts_dates", "employee_id", "start_date", "end_date"),
    )


class EmployeeLifecycleEvent(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Append-only history of significant changes to an employee.

    Distinct from `audit_logs`: the audit log records *who did what to which
    row*, while this records *what happened to this person's employment* in
    business terms. A payroll dispute is answered from here.
    """

    __tablename__ = "employee_lifecycle_events"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[LifecycleEventType] = mapped_column(
        status_enum(LifecycleEventType, "lifecycle_event_type", 32), nullable=False
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    previous_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    new_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID | None] = mapped_column()

    __table_args__ = (
        Index("ix_employee_lifecycle_events_employee", "employee_id", "effective_date"),
        Index("ix_employee_lifecycle_events_company_type", "company_id", "event_type"),
    )
