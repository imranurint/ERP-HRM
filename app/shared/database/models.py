"""ORM model registry.

Importing this package registers every table on `Base.metadata`, which is what
Alembic autogenerate and the test bootstrap rely on. Import models from here
rather than from their individual modules.
"""

from __future__ import annotations

from app.modules.attendance.infrastructure.models import (
    AttendanceRecord,
    EmployeeShiftAssignment,
    Shift,
)
from app.modules.employees.infrastructure.models import Employee as Employee
from app.modules.employees.infrastructure.models import (
    EmployeeLifecycleEvent,
    EmploymentContract,
)
from app.modules.identity.infrastructure.models import (
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.modules.leave.infrastructure.models import (
    EmployeeLeavePolicy,
    LeaveBalance,
    LeavePolicy,
    LeavePolicyRule,
    LeaveRequest,
    LeaveTransaction,
    LeaveType,
)
from app.modules.organization.infrastructure.models import (
    Company,
    Currency,
    Department,
    Designation,
    Holiday,
    Location,
)
from app.modules.payroll.infrastructure.models import (
    PayrollPeriod,
    PayrollRun,
    Payslip,
    PayslipItem,
)
from app.modules.salary.infrastructure.models import (
    EmployeeSalaryComponent,
    EmployeeSalaryStructure,
    SalaryComponent,
)
from app.shared.audit.models import AuditLog
from app.shared.database.base import Base
from app.shared.outbox.models import OutboxEvent
from app.shared.sequences.models import DocumentSequence

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
