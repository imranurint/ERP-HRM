# API examples

**Every response below is real captured output** from a live server running
against PostgreSQL 16, seeded with `make seed`. Tokens are truncated and
`request_id` is elided; nothing else is edited.

Setup used for this transcript:

```bash
make install && make migrate && make seed
make create-admin email=admin@example.com
make run                                    # http://localhost:8001
```

```bash
API=http://localhost:8001/api/v1
```

---

## 1. Authenticate

```bash
curl -s -X POST $API/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"demo-admin-password-2026"}'
```

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ...",
  "refresh_token": "g7SCGA5KVmNCD1v6u8wyCxsLieG5...",
  "token_type": "bearer",
  "expires_at": "2026-09-02T12:40:03.922722Z",
  "expires_in": 1800
}
```

Everything below assumes:

```bash
TOKEN=<access_token>
COMPANY=<company id from GET /companies>
H="Authorization: Bearer $TOKEN"
```

Refreshing rotates the token — the presented one is revoked, and replaying it
returns `401 INVALID_REFRESH_TOKEN`.

---

## 2. Create an employee

Omit `employee_number` and one is allocated atomically.

```bash
curl -s -X POST "$API/employees?company_id=$COMPANY" -H "$H" \
  -H 'Content-Type: application/json' -d '{
    "first_name": "Barbara",
    "last_name": "Liskov",
    "work_email": "barbara.liskov@demo.example",
    "hire_date": "2026-02-02"
  }'
```

```json
{
  "id": "01a06206-bd85-7014-a15c-d6c1de42254e",
  "employee_number": "EMP-2026-000006",
  "full_name": "Barbara Liskov",
  "work_email": "barbara.liskov@demo.example",
  "hire_date": "2026-02-02",
  "employment_status": "PROBATION",
  "is_active": true,
  "version": 1
}
```

### A duplicate work email is a 409

```json
{
  "error": "DUPLICATE_WORK_EMAIL",
  "message": "That work email address is already in use in this company.",
  "details": { "work_email": "barbara.liskov@demo.example" },
  "request_id": "..."
}
```

Every error in the system has that shape: `error`, `message`, `details`,
`request_id`.

---

## 3. Request and approve leave

Friday 6 March to Monday 9 March — four calendar days, **two** working days.

```bash
curl -s -X POST "$API/leave-requests?company_id=$COMPANY" -H "$H" \
  -H 'Content-Type: application/json' -d "{
    \"employee_id\": \"$ADA\",
    \"leave_type_id\": \"$ANNUAL\",
    \"start_date\": \"2026-03-06\",
    \"end_date\": \"2026-03-09\",
    \"reason\": \"Long weekend\"
  }"
```

```json
{
  "id": "01a06206-fcab-702a-8c19-e04a3829d626",
  "leave_code": "ANNUAL",
  "start_date": "2026-03-06",
  "end_date": "2026-03-09",
  "total_days": "2.00",
  "status": "PENDING"
}
```

```bash
curl -s -X POST "$API/leave-requests/$LEAVE/approve?company_id=$COMPANY" -H "$H"
```

```json
{
  "id": "01a06206-fcab-702a-8c19-e04a3829d626",
  "status": "APPROVED",
  "total_days": "2.00",
  "approved_at": "2026-09-02T12:10:20.243152Z"
}
```

### The balance afterwards

```bash
curl -s "$API/leave-balances?company_id=$COMPANY&employee_id=$ADA&year=2026" -H "$H"
```

```json
[
  {
    "leave_code": "ANNUAL",
    "leave_name": "Annual Leave",
    "year": 2026,
    "opening_balance": "0.00",
    "earned": "25.00",
    "used": "2.00",
    "adjustment": "0.00",
    "closing_balance": "23.00",
    "pending_days": "0.00",
    "available": "23.00"
  }
]
```

`closing_balance = opening + earned − used + adjustment`, verified by a CHECK
constraint on every write. `available` additionally subtracts days held by
pending requests.

### Over-budget requests are refused with the arithmetic

```json
{
  "error": "LEAVE_BALANCE_INSUFFICIENT",
  "message": "Requesting 196.00 days of ANNUAL but only 25.00 are available.",
  "details": {
    "leave_code": "ANNUAL",
    "requested_days": "196.00",
    "available_days": "25.00",
    "shortfall_days": "171.00"
  },
  "request_id": "..."
}
```

---

## 4. Run payroll

```bash
curl -s -X POST "$API/payroll-runs?company_id=$COMPANY" -H "$H" \
  -H 'Content-Type: application/json' \
  -d "{\"payroll_period_id\":\"$PERIOD\",\"notes\":\"March 2026 monthly run\"}"
```

```json
{
  "id": "01a06207-32ed-70f2-b121-e1e8bf34a284",
  "run_number": "PR-2026-000001",
  "status": "DRAFT",
  "currency_code": "GBP",
  "payslip_count": 0
}
```

```bash
curl -s -X POST "$API/payroll-runs/$RUN/calculate?company_id=$COMPANY" -H "$H" \
  -H 'Content-Type: application/json' -d '{}'
```

```json
{
  "run_number": "PR-2026-000001",
  "status": "COMPLETED",
  "payslip_count": 5,
  "total_gross": "30806.4300",
  "total_deductions": "5701.6100",
  "total_employer_contributions": "4251.2900",
  "total_net": "25104.8200",
  "currency_code": "GBP"
}
```

The whole run is one transaction: five payslips, their items, the totals, the
audit row and the outbox event either all land or none do.

---

## 5. Preview the accounting journal

Read-only, no side effects. This is exactly what `PayrollApproved` will carry
to the Accounting module.

```bash
curl -s "$API/payroll-runs/$RUN/journal-preview?company_id=$COMPANY" -H "$H"
```

```json
{
  "run_number": "PR-2026-000001",
  "currency_code": "GBP",
  "total_debits": "35057.7200",
  "total_credits": "35057.7200",
  "balanced": true,
  "lines": [
    { "gl_account_code": "6000", "side": "DEBIT",  "amount": "26095.4500", "component_code": "BASIC",     "description": "Payroll PR-2026-000001: BASIC (earning)" },
    { "gl_account_code": "6010", "side": "DEBIT",  "amount": "3945.0000",  "component_code": "HOUSING",   "description": "Payroll PR-2026-000001: HOUSING (earning)" },
    { "gl_account_code": "6020", "side": "DEBIT",  "amount": "750.0000",   "component_code": "TRANSPORT", "description": "Payroll PR-2026-000001: TRANSPORT (earning)" },
    { "gl_account_code": "6030", "side": "DEBIT",  "amount": "15.9800",    "component_code": "OVERTIME",  "description": "Payroll PR-2026-000001: OVERTIME (earning)" },
    { "gl_account_code": "6040", "side": "DEBIT",  "amount": "4251.2900",  "component_code": "NI_ER",     "description": "Payroll PR-2026-000001: NI_ER (employer cost)" },
    { "gl_account_code": "2200", "side": "CREDIT", "amount": "1540.3200",  "component_code": "PENSION",   "description": "Payroll PR-2026-000001: PENSION (deduction)" },
    { "gl_account_code": "2210", "side": "CREDIT", "amount": "4161.2900",  "component_code": "TAX",       "description": "Payroll PR-2026-000001: TAX (deduction)" },
    { "gl_account_code": "2100", "side": "CREDIT", "amount": "25104.8200", "component_code": "NET_PAY",   "description": "Payroll PR-2026-000001: net pay payable" },
    { "gl_account_code": "2110", "side": "CREDIT", "amount": "4251.2900",  "component_code": "EMPLOYER_LIABILITY", "description": "Payroll PR-2026-000001: employer contributions payable" }
  ]
}
```

Debits equal credits by construction:
`gross (30806.43) + employer (4251.29) = 35057.72` on both sides.

---

## 6. A payslip

Ada had one absent day in a 22-working-day month, so her basic is prorated to
21/22 of £4500 = £4295.45.

```bash
curl -s "$API/reports/payslip?company_id=$COMPANY&payslip_id=$PAYSLIP" -H "$H"
```

```json
{
  "employee_number": "EMP-2026-000001",
  "full_name": "Ada Lovelace",
  "period_name": "2026-03",
  "period_start": "2026-03-01",
  "period_end": "2026-03-31",
  "pay_date": "2026-04-05",
  "currency_code": "GBP",
  "earnings": [
    { "component_code": "BASIC",     "component_name": "Basic Salary",        "amount": "4295.4500", "is_taxable": true },
    { "component_code": "HOUSING",   "component_name": "Housing Allowance",   "amount": "675.0000",  "is_taxable": true },
    { "component_code": "TRANSPORT", "component_name": "Transport Allowance", "amount": "150.0000",  "is_taxable": true },
    { "component_code": "OVERTIME",  "component_name": "Overtime",            "amount": "15.9800",   "is_taxable": true }
  ],
  "deductions": [
    { "component_code": "PENSION", "component_name": "Pension (employee)", "amount": "256.8200", "is_taxable": false },
    { "component_code": "TAX",     "component_name": "Income Tax",         "amount": "627.2900", "is_taxable": false }
  ],
  "employer_contributions": [
    { "component_code": "NI_ER", "component_name": "Employer NI", "amount": "708.8300", "is_taxable": false }
  ],
  "gross_salary": "5136.4300",
  "total_deductions": "884.1100",
  "total_employer_contributions": "708.8300",
  "net_salary": "4252.3200",
  "payable_days": "21.00",
  "working_days": "22"
}
```

Note that `HOUSING` is £675 — 15% of the **contractual** £4500, not of the
prorated basic. An allowance should not shrink because somebody took a day off.

---

## 7. Approve, then find it immutable

```bash
curl -s -X POST "$API/payroll-runs/$RUN/approve?company_id=$COMPANY" -H "$H"
```

```json
{
  "run_number": "PR-2026-000001",
  "status": "APPROVED",
  "payslip_count": 5,
  "total_net": "25104.8200",
  "approved_at": "2026-09-02T12:14:28.815255Z"
}
```

Recalculating it now (Golden Rule 2):

```json
{
  "error": "PAYROLL_ALREADY_APPROVED",
  "message": "Approved payroll cannot be modified.",
  "details": { "status": "APPROVED" },
  "request_id": "..."
}
```

```bash
curl -s -X POST "$API/payroll-runs/$RUN/pay?company_id=$COMPANY" -H "$H"
```

```json
{
  "run_number": "PR-2026-000001",
  "status": "PAID",
  "total_net": "25104.8200",
  "paid_at": "2026-09-02T12:14:28.873241Z"
}
```

### The source data is frozen too

Amending attendance underneath a processed period (Golden Rule 8):

```json
{
  "error": "ATTENDANCE_LOCKED",
  "message": "Attendance falls in a payroll period that is PAID and can no longer change.",
  "details": { "payroll_period_status": "PAID", "entity": "Attendance" },
  "request_id": "..."
}
```

To correct an approved run, create a **new** run with `adjusts_run_id` set to
the original — both stay visible.

---

## 8. Reports, JSON or CSV

Add `&response_format=csv` to any report.

```bash
curl -s "$API/reports/payroll-summary?company_id=$COMPANY&run_id=$RUN&response_format=csv" -H "$H"
```

```csv
employee_number,full_name,gross_salary,total_deductions,employer_contributions,net_salary,currency_code,status
EMP-2026-000001,Ada Lovelace,5136.4300,884.1100,708.8300,4252.3200,GBP,PAID
EMP-2026-000002,Grace Hopper,5670.0000,1017.5000,782.4600,4652.5000,GBP,PAID
EMP-2026-000003,Alan Turing,10500.0000,2225.0000,1449.0000,8275.0000,GBP,PAID
EMP-2026-000004,Katherine Johnson,4520.0000,730.0000,623.7600,3790.0000,GBP,PAID
EMP-2026-000005,Mary Jackson,4980.0000,845.0000,687.2400,4135.0000,GBP,PAID
TOTAL,,30806.4300,5701.6100,4251.2900,25104.8200,,
```

Response headers:

```
content-disposition: attachment; filename="payroll-summary.csv"
content-type: text/csv; charset=utf-8
x-ratelimit-limit: 300
x-ratelimit-remaining: 298
```

Available reports: `employee-directory`, `department-headcount`,
`attendance-summary`, `late-attendance`, `leave-balances`, `leave-usage`,
`payroll-summary`, `payslip`, `salary-components`, `employee-lifecycle`.

---

## 9. What landed in the database

Domain events, written in the same transactions as the changes they describe:

```
$ psql -c "SELECT event_type, aggregate_type, status FROM outbox_events ORDER BY created_at;"

    event_type     | aggregate_type | status
-------------------+----------------+---------
 EmployeeHired     | Employee       | PENDING
 LeaveRequested    | LeaveRequest   | PENDING
 LeaveApproved     | LeaveRequest   | PENDING
 PayrollCalculated | PayrollRun     | PENDING
 PayrollApproved   | PayrollRun     | PENDING
 PayrollPaid       | PayrollRun     | PENDING
```

They stay `PENDING` because the relay process is deliberately not part of this
module — see the README's scope section.

The audit trail for the run:

```
$ psql -c "SELECT action, entity_type, new_data->>'status' AS status
           FROM audit_logs WHERE entity_type='PayrollRun' ORDER BY created_at;"

       action        | entity_type |  status
---------------------+-------------+----------
 PAYROLL_RUN_CREATED | PayrollRun  |
 PAYROLL_CALCULATED  | PayrollRun  |
 PAYROLL_APPROVED    | PayrollRun  | APPROVED
 PAYROLL_PAID        | PayrollRun  | PAID
```

---

## 10. Prove the leave balances are derivable

```bash
make rebuild-leave-balances
```

```
01a06206-8f52-703d-b5a5-232b7d1c7566: replayed 11 transactions into 10 balances (cleared 10, mismatches 0)
matched_previous=True
```

The projection was dropped and rebuilt from `leave_transactions` alone, through
the same function the live path uses, and came back identical. Exits non-zero
on a mismatch, so it works as a scheduled integrity check.

---

## Health

```bash
curl -s http://localhost:8001/readyz
```

```json
{
  "status": "ready",
  "version": "ERP HRM Core",
  "environment": "local",
  "timestamp": "2026-09-02T12:09:29.919433Z",
  "checks": {
    "database": { "status": "ok", "required": true },
    "redis": { "status": "ok", "required": false }
  }
}
```

Redis is `required: false` — rate limiting and caching degrade gracefully, and
refusing traffic because a cache is down would be worse than serving it
uncached. A database outage does fail readiness.
