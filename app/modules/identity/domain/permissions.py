"""Permissions and default roles.

Business logic checks *permissions*, never role names. Roles are just bundles
of permissions and are expected to be edited per deployment; the permission
constants below are the stable contract.

The payroll permissions are deliberately split four ways
(`read`/`calculate`/`approve`/`pay`) so a deployment can enforce separation of
duties: the person who runs the numbers need not be the person who approves
them, and neither need be the person who releases the money.
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    EMPLOYEES_READ = "employees:read"
    EMPLOYEES_WRITE = "employees:write"
    EMPLOYEES_MANAGE = "employees:manage"
    ATTENDANCE_READ = "attendance:read"
    ATTENDANCE_WRITE = "attendance:write"
    LEAVE_READ = "leave:read"
    LEAVE_APPROVE = "leave:approve"
    LEAVE_MANAGE = "leave:manage"
    PAYROLL_READ = "payroll:read"
    PAYROLL_CALCULATE = "payroll:calculate"
    PAYROLL_APPROVE = "payroll:approve"
    PAYROLL_PAY = "payroll:pay"
    ORGANIZATION_MANAGE = "organization:manage"
    REPORTS_READ = "reports:read"
    USERS_MANAGE = "users:manage"


PERMISSION_DESCRIPTIONS: dict[Permission, str] = {
    Permission.EMPLOYEES_READ: "Read employee records and organisation structure",
    Permission.EMPLOYEES_WRITE: "Create and edit employee records",
    Permission.EMPLOYEES_MANAGE: "Drive employee lifecycle: activate, transfer, promote, terminate",
    Permission.ATTENDANCE_READ: "Read attendance records",
    Permission.ATTENDANCE_WRITE: "Record and amend attendance",
    Permission.LEAVE_READ: "Read leave requests and balances",
    Permission.LEAVE_APPROVE: "Approve or reject leave requests",
    Permission.LEAVE_MANAGE: "Manage leave types, policies and balance adjustments",
    Permission.PAYROLL_READ: "Read payroll runs and payslips",
    Permission.PAYROLL_CALCULATE: "Create payroll runs and calculate payslips",
    Permission.PAYROLL_APPROVE: "Approve a calculated payroll run",
    Permission.PAYROLL_PAY: "Mark an approved payroll run as paid",
    Permission.ORGANIZATION_MANAGE: "Manage companies, departments, designations and locations",
    Permission.REPORTS_READ: "Run HR and payroll reports",
    Permission.USERS_MANAGE: "Manage users and role assignments",
}


class Role(StrEnum):
    ADMIN = "Admin"
    HR_MANAGER = "HR Manager"
    HR_OFFICER = "HR Officer"
    PAYROLL_MANAGER = "Payroll Manager"
    MANAGER = "Manager"
    EMPLOYEE = "Employee"
    AUDITOR = "Auditor"
    VIEWER = "Viewer"


#: Default role → permission mapping installed by `python -m scripts.seed`.
DEFAULT_ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.HR_MANAGER: frozenset(
        {
            Permission.EMPLOYEES_READ,
            Permission.EMPLOYEES_WRITE,
            Permission.EMPLOYEES_MANAGE,
            Permission.ATTENDANCE_READ,
            Permission.ATTENDANCE_WRITE,
            Permission.LEAVE_READ,
            Permission.LEAVE_APPROVE,
            Permission.LEAVE_MANAGE,
            Permission.PAYROLL_READ,
            Permission.ORGANIZATION_MANAGE,
            Permission.REPORTS_READ,
        }
    ),
    Role.HR_OFFICER: frozenset(
        {
            Permission.EMPLOYEES_READ,
            Permission.EMPLOYEES_WRITE,
            Permission.ATTENDANCE_READ,
            Permission.ATTENDANCE_WRITE,
            Permission.LEAVE_READ,
            Permission.REPORTS_READ,
        }
    ),
    # Can run and approve payroll, but cannot edit the employee data that
    # feeds it — the separation that makes payroll approval meaningful.
    Role.PAYROLL_MANAGER: frozenset(
        {
            Permission.EMPLOYEES_READ,
            Permission.ATTENDANCE_READ,
            Permission.LEAVE_READ,
            Permission.PAYROLL_READ,
            Permission.PAYROLL_CALCULATE,
            Permission.PAYROLL_APPROVE,
            Permission.PAYROLL_PAY,
            Permission.REPORTS_READ,
        }
    ),
    Role.MANAGER: frozenset(
        {
            Permission.EMPLOYEES_READ,
            Permission.ATTENDANCE_READ,
            Permission.LEAVE_READ,
            Permission.LEAVE_APPROVE,
            Permission.REPORTS_READ,
        }
    ),
    # Self-service only. Every route an Employee can reach scopes itself to
    # the caller's own employee record.
    Role.EMPLOYEE: frozenset(
        {
            Permission.ATTENDANCE_WRITE,
            Permission.LEAVE_READ,
        }
    ),
    # An auditor observes; it must never be able to change HR or payroll data.
    Role.AUDITOR: frozenset(
        {
            Permission.EMPLOYEES_READ,
            Permission.ATTENDANCE_READ,
            Permission.LEAVE_READ,
            Permission.PAYROLL_READ,
            Permission.REPORTS_READ,
        }
    ),
    Role.VIEWER: frozenset({Permission.EMPLOYEES_READ, Permission.REPORTS_READ}),
}

ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.ADMIN: "Full administrative control including configuration and users",
    Role.HR_MANAGER: "Owns the employee lifecycle, leave policy and org structure",
    Role.HR_OFFICER: "Day-to-day HR administration without lifecycle or policy authority",
    Role.PAYROLL_MANAGER: "Calculates, approves and pays payroll; cannot edit employee data",
    Role.MANAGER: "Line manager: reads their people's data and approves leave",
    Role.EMPLOYEE: "Self-service: own attendance and own leave",
    Role.AUDITOR: "Read-only access to all HR and payroll data",
    Role.VIEWER: "Read-only access for general business users",
}
