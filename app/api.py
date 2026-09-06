"""API v1 router composition.

Modules expose routers; this file is the only place that knows the full set.
Adding a module is one import and one `include_router`.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.modules.attendance.api import routes as attendance
from app.modules.employees.api import routes as employees
from app.modules.identity.api import routes as identity
from app.modules.leave.api import routes as leave
from app.modules.organization.api import routes as organization
from app.modules.payroll.api import routes as payroll
from app.modules.reports.api import routes as reports
from app.modules.salary.api import routes as salary

api_router = APIRouter()

api_router.include_router(identity.router)
api_router.include_router(organization.company_router)
api_router.include_router(organization.router)
api_router.include_router(employees.router)
api_router.include_router(employees.contract_router)
api_router.include_router(salary.router)
api_router.include_router(attendance.router)
api_router.include_router(leave.router)
api_router.include_router(payroll.router)
api_router.include_router(reports.router)
