"""Company and organisation structure.

`company_id` on every table below is the multi-company isolation boundary.
Nothing in this system joins across companies, and every unique constraint is
scoped by it — two companies may both have a department coded `ENG` without
colliding.
"""

from __future__ import annotations

from datetime import date
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.shared.database.base import (
    ActorTrackingMixin,
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class Currency(Base, TimestampMixin):
    """ISO-4217 currency.

    The three-letter code is the natural primary key: it is stable, globally
    agreed, and readable in raw SQL. `decimal_places` is what payroll rounds
    to — JPY has none, KWD has three, and getting that wrong is a visible
    error on every payslip.
    """

    __tablename__ = "currencies"

    currency_code: Mapped[str] = mapped_column(String(3), primary_key=True)
    currency_name: Mapped[str] = mapped_column(String(100), nullable=False)
    decimal_places: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint(
            "currency_code = upper(currency_code) AND length(currency_code) = 3",
            name="currency_code_format",
        ),
        CheckConstraint(
            "decimal_places >= 0 AND decimal_places <= 4",
            name="decimal_places_range",
        ),
    )


class Company(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """A legal entity that employs people and runs its own payroll."""

    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(255), nullable=False)
    registration_number: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    address: Mapped[str | None] = mapped_column(Text)
    #: IANA zone. Attendance is captured in UTC but *filed* against the
    #: calendar date this timezone says it is — an 01:00 UTC check-in in
    #: Auckland belongs to a different day than it does in London.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    base_currency_code: Mapped[str] = mapped_column(
        String(3),
        ForeignKey("currencies.currency_code", ondelete="RESTRICT"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    base_currency: Mapped[Currency] = relationship(lazy="joined")

    __table_args__ = (Index("ix_companies_active", "is_active"),)


class Department(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """A hierarchical department.

    `parent_department_id` is self-referential and cycle-checked in the
    application (`ensure_no_department_cycle`) — PostgreSQL cannot express
    "acyclic" as a constraint, so the guard has to live in the service that
    walks the ancestor chain.
    """

    __tablename__ = "departments"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    parent_department_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT")
    )
    department_code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: No FK to employees: the two tables reference each other, and a circular
    #: FK pair makes both inserts and deletes require deferred constraints for
    #: no real benefit. Referential integrity here is enforced by the service.
    manager_employee_id: Mapped[UUID | None] = mapped_column()
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    parent: Mapped[Department | None] = relationship(remote_side="Department.id", lazy="noload")

    __table_args__ = (
        UniqueConstraint(
            "company_id", "department_code", name="uq_departments_company_id_department_code"
        ),
        CheckConstraint("id <> parent_department_id", name="not_own_parent"),
        CheckConstraint("length(trim(department_code)) > 0", name="department_code_not_blank"),
        Index("ix_departments_company_active", "company_id", "is_active"),
        Index("ix_departments_parent", "parent_department_id"),
    )


class Designation(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """A job title. Separate from department: a Software Engineer can sit in
    Engineering or in R&D, and moving between them is a transfer, not a
    promotion."""

    __tablename__ = "designations"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    designation_code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: Higher means more senior. Lets "promote" be validated rather than
    #: taken on trust, and lets reports order a career path sensibly.
    grade_level: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint(
            "company_id", "designation_code", name="uq_designations_company_id_designation_code"
        ),
        CheckConstraint("length(trim(designation_code)) > 0", name="designation_code_not_blank"),
        CheckConstraint(
            "grade_level IS NULL OR grade_level BETWEEN 1 AND 100", name="grade_level_range"
        ),
        Index("ix_designations_company_active", "company_id", "is_active"),
    )


class Location(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """A work site.

    Carries its own optional timezone, which overrides the company's. A
    company headquartered in London with a Manila office needs both, or the
    Manila team's attendance lands on the wrong day.
    """

    __tablename__ = "locations"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    location_code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint(
            "company_id", "location_code", name="uq_locations_company_id_location_code"
        ),
        CheckConstraint("length(trim(location_code)) > 0", name="location_code_not_blank"),
        Index("ix_locations_company_active", "company_id", "is_active"),
    )


class Holiday(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A public or company holiday.

    Not in the original specification, but leave-day counting and attendance
    classification are both wrong without it: booking Thursday–Tuesday over a
    bank holiday must not consume that day of entitlement. Scoped optionally
    to a location so a multi-country company can hold different calendars.
    """

    __tablename__ = "holidays"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    location_id: Mapped[UUID | None] = mapped_column(ForeignKey("locations.id", ondelete="CASCADE"))
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        # A company-wide holiday and a location-specific one may share a date,
        # so the uniqueness that matters is per (company, location, date).
        UniqueConstraint(
            "company_id",
            "location_id",
            "holiday_date",
            name="uq_holidays_company_id_location_id_holiday_date",
        ),
        Index("ix_holidays_company_date", "company_id", "holiday_date"),
        Index("ix_holidays_location", "location_id"),
    )


__all__ = ["Company", "Currency", "Department", "Designation", "Holiday", "Location"]
