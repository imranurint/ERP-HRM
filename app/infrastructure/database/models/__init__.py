"""ORM model registry.

Importing this package registers every table on `Base.metadata`, which is what
Alembic autogenerate and the test bootstrap rely on. Import models from here
rather than from their individual modules.
"""

from __future__ import annotations

from app.infrastructure.database.base import Base
from app.infrastructure.database.models.attendance import (
    AttendanceRecord,
    EmployeeShiftAssignment,
    Shift,
)
from app.infrastructure.database.models.audit import AuditLog
from app.infrastructure.database.models.employee import Employee as Employee
from app.infrastructure.database.models.employee import (
    EmployeeLifecycleEvent,
    EmploymentContract,
)
from app.infrastructure.database.models.identity import (
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.infrastructure.database.models.leave import (
    EmployeeLeavePolicy,
    LeaveBalance,
    LeavePolicy,
    LeavePolicyRule,
    LeaveRequest,
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
from app.infrastructure.database.models.payroll import (
    PayrollPeriod,
    PayrollRun,
    Payslip,
    PayslipItem,
)
from app.infrastructure.database.models.salary import (
    EmployeeSalaryComponent,
    EmployeeSalaryStructure,
    SalaryComponent,
)
from app.infrastructure.database.models.sequence import DocumentSequence
from app.infrastructure.outbox.models import OutboxEvent

__all__ = [
    "AttendanceRecord",
    "AuditLog",
    "Base",
    "Company",
    "Currency",
    "Department",
    "Designation",
    "DocumentSequence",
    "Employee",
    "EmployeeLeavePolicy",
    "EmployeeLifecycleEvent",
    "EmployeeSalaryComponent",
    "EmployeeSalaryStructure",
    "EmployeeShiftAssignment",
    "EmploymentContract",
    "Holiday",
    "LeaveBalance",
    "LeavePolicy",
    "LeavePolicyRule",
    "LeaveRequest",
    "LeaveTransaction",
    "LeaveType",
    "Location",
    "OutboxEvent",
    "PayrollPeriod",
    "PayrollRun",
    "Payslip",
    "PayslipItem",
    "Permission",
    "RefreshToken",
    "Role",
    "RolePermission",
    "SalaryComponent",
    "Shift",
    "User",
    "UserRole",
]
