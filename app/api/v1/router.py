"""API v1 router composition."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    attendance,
    auth,
    companies,
    contracts,
    employees,
    leave,
    organization,
    payroll,
    reports,
    salary,
)

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(companies.router)
api_router.include_router(organization.router)
api_router.include_router(employees.router)
api_router.include_router(contracts.router)
api_router.include_router(salary.router)
api_router.include_router(attendance.router)
api_router.include_router(leave.router)
api_router.include_router(payroll.router)
api_router.include_router(reports.router)
