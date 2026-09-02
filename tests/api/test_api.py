"""HTTP-layer tests.

These exercise routing, serialisation, authorisation and the error envelope —
the things that only exist above the service layer. Business rules have their
own integration tests and are not re-asserted here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import DemoData, make_admin_token

pytestmark = [pytest.mark.api, pytest.mark.asyncio]


@pytest.fixture
async def auth(client: AsyncClient, session: AsyncSession) -> dict[str, str]:
    token = await make_admin_token(client, session)
    return {"Authorization": f"Bearer {token}"}


class TestHealth:
    async def test_livez_needs_no_auth(self, client: AsyncClient) -> None:
        response = await client.get("/livez")
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}

    async def test_readyz_reports_dependencies(self, client: AsyncClient) -> None:
        response = await client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["checks"]["database"]["status"] == "ok"
        # Redis is optional: degraded must not fail readiness.
        assert body["checks"]["redis"]["required"] is False

    async def test_every_response_carries_a_request_id(self, client: AsyncClient) -> None:
        response = await client.get("/livez")
        assert response.headers["X-Request-ID"]
        assert response.headers["X-Correlation-ID"]

    async def test_an_inbound_correlation_id_is_propagated(self, client: AsyncClient) -> None:
        """One id survives across ERP modules — HR action to payroll journal."""
        response = await client.get("/livez", headers={"X-Correlation-ID": "trace-123"})
        assert response.headers["X-Correlation-ID"] == "trace-123"


class TestAuth:
    async def test_login_returns_a_token_pair(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        await make_admin_token(client, session)
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "apitest.admin@test.example", "password": "test-password-1234"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"] and body["refresh_token"]

    async def test_bad_credentials_are_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@test.example", "password": "wrong-password-here"},
        )
        assert response.status_code == 401
        assert response.json()["error"] == "INVALID_CREDENTIALS"

    async def test_an_unknown_email_gives_the_same_error_as_a_bad_password(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        """The response must not reveal which emails exist."""
        await make_admin_token(client, session)
        unknown = await client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@test.example", "password": "test-password-1234"},
        )
        wrong = await client.post(
            "/api/v1/auth/login",
            json={"email": "apitest.admin@test.example", "password": "definitely-wrong-1"},
        )
        assert unknown.json()["error"] == wrong.json()["error"] == "INVALID_CREDENTIALS"

    async def test_me_returns_the_caller(self, client: AsyncClient, auth: dict[str, str]) -> None:
        response = await client.get("/api/v1/auth/me", headers=auth)
        assert response.status_code == 200
        assert response.json()["email"] == "apitest.admin@test.example"

    async def test_refresh_rotates_the_token(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        await make_admin_token(client, session)
        login = await client.post(
            "/api/v1/auth/login",
            json={"email": "apitest.admin@test.example", "password": "test-password-1234"},
        )
        original = login.json()["refresh_token"]

        refreshed = await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
        assert refreshed.status_code == 200
        assert refreshed.json()["refresh_token"] != original

        # The old token is now revoked; replaying it must fail.
        replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
        assert replay.status_code == 401
        assert replay.json()["error"] == "INVALID_REFRESH_TOKEN"

    async def test_protected_routes_require_a_token(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/companies")
        assert response.status_code == 401
        assert response.json()["error"] == "UNAUTHORIZED"

    async def test_a_garbage_token_is_rejected(self, client: AsyncClient) -> None:
        response = await client.get(
            "/api/v1/companies", headers={"Authorization": "Bearer not-a-real-jwt"}
        )
        assert response.status_code == 401
        assert response.json()["error"] == "INVALID_TOKEN"


class TestErrorEnvelope:
    async def test_every_error_has_the_same_shape(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/companies")
        body = response.json()
        assert set(body) == {"error", "message", "details", "request_id"}

    async def test_validation_errors_are_structured(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        response = await client.post("/api/v1/companies", json={"name": ""}, headers=auth)
        assert response.status_code == 422
        assert response.json()["error"] == "VALIDATION_ERROR"
        assert response.json()["details"]["errors"]

    async def test_a_missing_resource_is_a_clean_404(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        response = await client.get(
            "/api/v1/companies/01a061f8-0000-7000-8000-000000000000", headers=auth
        )
        assert response.status_code == 404
        assert response.json()["error"] == "NOT_FOUND"


class TestCompaniesAndEmployees:
    async def test_create_and_fetch_a_company(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.post(
            "/api/v1/companies",
            json={
                "name": "Second Company",
                "legal_name": "Second Company Ltd.",
                "base_currency_code": "GBP",
                "timezone": "Europe/London",
            },
            headers=auth,
        )
        assert response.status_code == 201
        company_id = response.json()["id"]

        fetched = await client.get(f"/api/v1/companies/{company_id}", headers=auth)
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "Second Company"

    async def test_an_unknown_timezone_is_rejected(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        """A typo would file a whole office's attendance on the wrong day."""
        response = await client.post(
            "/api/v1/companies",
            json={
                "name": "Bad TZ",
                "legal_name": "Bad TZ Ltd.",
                "base_currency_code": "GBP",
                "timezone": "Mars/Olympus_Mons",
            },
            headers=auth,
        )
        assert response.status_code == 422
        assert response.json()["error"] == "VALIDATION_ERROR"

    async def test_create_an_employee_with_an_allocated_number(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.post(
            f"/api/v1/employees?company_id={demo.company_id}",
            json={
                "first_name": "New",
                "last_name": "Starter",
                "work_email": "new.starter@test.example",
                "hire_date": "2026-02-02",
                "department_id": str(demo.departments["ENG"].id),
            },
            headers=auth,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["employee_number"].startswith("EMP-2026-")
        assert body["full_name"] == "New Starter"

    async def test_a_duplicate_work_email_is_a_409(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        payload = {
            "first_name": "Copy",
            "last_name": "Cat",
            "work_email": demo.employee.work_email,
            "hire_date": "2026-02-02",
        }
        response = await client.post(
            f"/api/v1/employees?company_id={demo.company_id}", json=payload, headers=auth
        )
        assert response.status_code == 409
        assert response.json()["error"] == "DUPLICATE_WORK_EMAIL"

    async def test_listing_is_paginated(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.get(
            f"/api/v1/employees?company_id={demo.company_id}&page=1&page_size=2", headers=auth
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 2
        assert body["total"] == len(demo.employees)
        assert body["page"] == 1

    async def test_search_filters_the_listing(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.get(
            f"/api/v1/employees?company_id={demo.company_id}&search=ada", headers=auth
        )
        assert response.status_code == 200
        assert all("Ada" in item["first_name"] for item in response.json()["items"])

    async def test_terminate_is_a_status_change_not_a_delete(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.post(
            f"/api/v1/employees/{demo.employee.id}/terminate?company_id={demo.company_id}",
            json={"termination_date": "2026-06-30", "reason": "End of project"},
            headers=auth,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["employment_status"] == "TERMINATED"
        assert body["is_active"] is False
        assert body["termination_date"] == "2026-06-30"

        # Still fetchable — history must remain joinable.
        fetched = await client.get(
            f"/api/v1/employees/{demo.employee.id}?company_id={demo.company_id}", headers=auth
        )
        assert fetched.status_code == 200

    async def test_lifecycle_history_records_the_termination(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        await client.post(
            f"/api/v1/employees/{demo.employee.id}/terminate?company_id={demo.company_id}",
            json={"termination_date": "2026-06-30", "resigned": True},
            headers=auth,
        )
        response = await client.get(
            f"/api/v1/employees/{demo.employee.id}/lifecycle?company_id={demo.company_id}",
            headers=auth,
        )
        assert response.status_code == 200
        events = {item["event_type"] for item in response.json()["items"]}
        assert "RESIGNED" in events

    async def test_promote_records_a_lifecycle_event(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.post(
            f"/api/v1/employees/{demo.employee.id}/promote?company_id={demo.company_id}",
            json={"designation_id": str(demo.designations["SM"].id), "reason": "Strong year"},
            headers=auth,
        )
        assert response.status_code == 200
        assert response.json()["designation_id"] == str(demo.designations["SM"].id)

    async def test_cross_company_access_is_refused(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData, session: AsyncSession
    ) -> None:
        """The multi-company isolation boundary, over HTTP."""
        other = await client.post(
            "/api/v1/companies",
            json={
                "name": "Rival Ltd",
                "legal_name": "Rival Ltd.",
                "base_currency_code": "GBP",
            },
            headers=auth,
        )
        rival_id = other.json()["id"]

        response = await client.get(
            f"/api/v1/employees/{demo.employee.id}?company_id={rival_id}", headers=auth
        )
        assert response.status_code == 404
        assert response.json()["error"] == "EMPLOYEE_NOT_FOUND"


class TestMoneyOnTheWire:
    async def test_money_is_a_string_in_responses(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.get(
            f"/api/v1/salary-structures?company_id={demo.company_id}"
            f"&employee_id={demo.employee.id}",
            headers=auth,
        )
        assert response.status_code == 200
        assert isinstance(response.json()[0]["base_salary"], str)

    async def test_a_json_number_for_money_is_rejected(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        """A float would let a rounding error into a salary before validation."""
        response = await client.post(
            f"/api/v1/salary-structures?company_id={demo.company_id}",
            json={
                "employee_id": str(demo.other_employee.id),
                "effective_from": "2027-01-01",
                "base_salary": 4500.55,
                "currency_code": "GBP",
            },
            headers=auth,
        )
        assert response.status_code == 422
        assert response.json()["error"] == "VALIDATION_ERROR"

    async def test_money_as_a_string_is_accepted(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.post(
            f"/api/v1/salary-structures?company_id={demo.company_id}",
            json={
                "employee_id": str(demo.other_employee.id),
                "effective_from": "2027-01-01",
                "base_salary": "4500.55",
                "currency_code": "GBP",
            },
            headers=auth,
        )
        assert response.status_code == 201
        # Returned as a string, exactly as sent — never a JSON number.
        body = response.json()
        assert isinstance(body["base_salary"], str)
        assert Decimal(body["base_salary"]) == Decimal("4500.55")


class TestPayrollOverHttp:
    async def test_the_full_run(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        base = f"?company_id={demo.company_id}"

        period = await client.post(
            f"/api/v1/payroll-periods{base}",
            json={
                "period_name": "2026-03",
                "start_date": "2026-03-01",
                "end_date": "2026-03-31",
                "pay_date": "2026-04-05",
            },
            headers=auth,
        )
        assert period.status_code == 201
        period_id = period.json()["id"]

        run = await client.post(
            f"/api/v1/payroll-runs{base}",
            json={"payroll_period_id": period_id},
            headers=auth,
        )
        assert run.status_code == 201
        run_id = run.json()["id"]
        assert run.json()["run_number"].startswith("PR-2026-")

        calculated = await client.post(
            f"/api/v1/payroll-runs/{run_id}/calculate{base}", json={}, headers=auth
        )
        assert calculated.status_code == 200
        assert calculated.json()["status"] == "COMPLETED"
        assert calculated.json()["payslip_count"] == len(demo.employees)

        preview = await client.get(
            f"/api/v1/payroll-runs/{run_id}/journal-preview{base}", headers=auth
        )
        assert preview.status_code == 200
        assert preview.json()["balanced"] is True

        approved = await client.post(f"/api/v1/payroll-runs/{run_id}/approve{base}", headers=auth)
        assert approved.status_code == 200
        assert approved.json()["status"] == "APPROVED"

        paid = await client.post(f"/api/v1/payroll-runs/{run_id}/pay{base}", headers=auth)
        assert paid.status_code == 200
        assert paid.json()["status"] == "PAID"

    async def test_recalculating_approved_payroll_is_a_409(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        base = f"?company_id={demo.company_id}"
        period = await client.post(
            f"/api/v1/payroll-periods{base}",
            json={
                "period_name": "2026-04",
                "start_date": "2026-04-01",
                "end_date": "2026-04-30",
            },
            headers=auth,
        )
        run = await client.post(
            f"/api/v1/payroll-runs{base}",
            json={"payroll_period_id": period.json()["id"]},
            headers=auth,
        )
        run_id = run.json()["id"]
        await client.post(f"/api/v1/payroll-runs/{run_id}/calculate{base}", json={}, headers=auth)
        await client.post(f"/api/v1/payroll-runs/{run_id}/approve{base}", headers=auth)

        response = await client.post(
            f"/api/v1/payroll-runs/{run_id}/calculate{base}", json={}, headers=auth
        )
        assert response.status_code == 409
        assert response.json()["error"] == "PAYROLL_ALREADY_APPROVED"

    async def test_overlapping_periods_are_a_409(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        base = f"?company_id={demo.company_id}"
        await client.post(
            f"/api/v1/payroll-periods{base}",
            json={
                "period_name": "2026-05",
                "start_date": "2026-05-01",
                "end_date": "2026-05-31",
            },
            headers=auth,
        )
        response = await client.post(
            f"/api/v1/payroll-periods{base}",
            json={
                "period_name": "2026-05-again",
                "start_date": "2026-05-15",
                "end_date": "2026-06-15",
            },
            headers=auth,
        )
        assert response.status_code == 409
        assert response.json()["error"] == "PAYROLL_PERIOD_OVERLAP"


class TestLeaveOverHttp:
    async def test_request_then_approve(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        base = f"?company_id={demo.company_id}"
        created = await client.post(
            f"/api/v1/leave-requests{base}",
            json={
                "employee_id": str(demo.employee.id),
                "leave_type_id": str(demo.leave_types["ANNUAL"].id),
                "start_date": "2026-03-02",
                "end_date": "2026-03-04",
            },
            headers=auth,
        )
        assert created.status_code == 201
        assert created.json()["total_days"] == "3.00"
        request_id = created.json()["id"]

        approved = await client.post(
            f"/api/v1/leave-requests/{request_id}/approve{base}", headers=auth
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "APPROVED"

        balances = await client.get(
            f"/api/v1/leave-balances{base}&employee_id={demo.employee.id}", headers=auth
        )
        annual = next(b for b in balances.json() if b["leave_code"] == "ANNUAL")
        assert annual["used"] == "3.00"
        assert annual["closing_balance"] == "22.00"

    async def test_an_over_budget_request_is_a_409(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.post(
            f"/api/v1/leave-requests?company_id={demo.company_id}",
            json={
                "employee_id": str(demo.employee.id),
                "leave_type_id": str(demo.leave_types["ANNUAL"].id),
                "start_date": "2026-04-01",
                "end_date": "2026-12-31",
            },
            headers=auth,
        )
        assert response.status_code == 409
        assert response.json()["error"] == "LEAVE_BALANCE_INSUFFICIENT"
        assert "shortfall_days" in response.json()["details"]


class TestReports:
    async def test_employee_directory_json(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.get(
            f"/api/v1/reports/employee-directory?company_id={demo.company_id}", headers=auth
        )
        assert response.status_code == 200
        body = response.json()
        assert body["meta"]["report"] == "employee_directory"
        assert body["meta"]["row_count"] == len(demo.employees)

    async def test_employee_directory_csv(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.get(
            f"/api/v1/reports/employee-directory?company_id={demo.company_id}&response_format=csv",
            headers=auth,
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        lines = response.text.strip().splitlines()
        assert lines[0].startswith("employee_number,full_name,work_email")
        assert len(lines) == len(demo.employees) + 1

    async def test_department_headcount(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.get(
            f"/api/v1/reports/department-headcount?company_id={demo.company_id}", headers=auth
        )
        assert response.status_code == 200
        assert response.json()["total_headcount"] == len(demo.employees)

    async def test_a_range_report_requires_its_dates(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.get(
            f"/api/v1/reports/attendance-summary?company_id={demo.company_id}", headers=auth
        )
        assert response.status_code == 422

    async def test_leave_balance_report_csv(
        self, client: AsyncClient, auth: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.get(
            f"/api/v1/reports/leave-balances?company_id={demo.company_id}"
            "&year=2026&response_format=csv",
            headers=auth,
        )
        assert response.status_code == 200
        assert "opening_balance" in response.text.splitlines()[0]


class TestPermissions:
    """Authorisation is checked here rather than repeated in every test."""

    @pytest.fixture
    async def viewer(self, client: AsyncClient, session: AsyncSession) -> dict[str, str]:
        from app.core.security import hash_password
        from app.domain.identity.permissions import Role
        from app.infrastructure.database.models.identity import User
        from app.infrastructure.database.unit_of_work import UnitOfWork
        from app.scripts.create_admin import ensure_roles_and_permissions

        uow = UnitOfWork(session)
        roles = await ensure_roles_and_permissions(uow)
        user = User(
            email="viewer@test.example",
            hashed_password=hash_password("viewer-password-123"),
            full_name="Read Only",
            is_active=True,
            is_superuser=False,
        )
        user.roles.append(roles[Role.VIEWER])
        uow.users.add(user)
        await uow.commit()

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": "viewer-password-123"},
        )
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    async def test_a_viewer_can_read_employees(
        self, client: AsyncClient, viewer: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.get(
            f"/api/v1/employees?company_id={demo.company_id}", headers=viewer
        )
        assert response.status_code == 200

    async def test_a_viewer_cannot_create_an_employee(
        self, client: AsyncClient, viewer: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.post(
            f"/api/v1/employees?company_id={demo.company_id}",
            json={
                "first_name": "Should",
                "last_name": "Fail",
                "work_email": "should.fail@test.example",
                "hire_date": "2026-02-02",
            },
            headers=viewer,
        )
        assert response.status_code == 403
        assert response.json()["error"] == "PERMISSION_DENIED"
        assert response.json()["details"]["required_permission"] == "employees:write"

    async def test_a_viewer_cannot_read_salary(
        self, client: AsyncClient, viewer: dict[str, str], demo: DemoData
    ) -> None:
        """Reading pay needs `payroll:read`, not `employees:read`."""
        response = await client.get(
            f"/api/v1/salary-structures?company_id={demo.company_id}"
            f"&employee_id={demo.employee.id}",
            headers=viewer,
        )
        assert response.status_code == 403

    async def test_a_viewer_cannot_approve_payroll(
        self, client: AsyncClient, viewer: dict[str, str], demo: DemoData
    ) -> None:
        response = await client.post(
            f"/api/v1/payroll-runs/01a061f8-0000-7000-8000-000000000000/approve"
            f"?company_id={demo.company_id}",
            headers=viewer,
        )
        assert response.status_code == 403


class TestOpenApi:
    async def test_the_schema_builds(self, client: AsyncClient) -> None:
        """Guards against a response model FastAPI cannot construct."""
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        assert len(response.json()["paths"]) > 50

    async def test_docs_are_served(self, client: AsyncClient) -> None:
        response = await client.get("/docs")
        assert response.status_code == 200
