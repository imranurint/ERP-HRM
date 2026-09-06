# ERP HRM Core

A production-grade HRM / HRIS backend: people, organisation structure,
employment contracts, attendance, leave and payroll.

Built as a **modular monolith**, sibling to the
[`../Accounting`](../Accounting) general ledger and to the same rules. An HR
action and its consequences commit in one database transaction, or not at all.

```
ERP/
├── Accounting/   double-entry general ledger  (the financial source of truth)
└── HRM/          this module                  (people, time and pay)
```

## What it guarantees

* **Payroll periods never overlap**, and neither do an employee's salary
  structures, shift assignments or leave-policy assignments — four GiST
  exclusion constraints, enforced by PostgreSQL rather than by application
  checks that lose races.
* **At most one active employment contract per employee** — a partial unique
  index, for the same reason.
* **Approved and paid payroll is immutable.** Corrections are new adjustment
  runs; history is never edited.
* **Every payslip stores the complete snapshot of inputs it came from**, so it
  can be explained years later without depending on current configuration.
* **`leave_balances` is a projection**, fully rebuildable from
  `leave_transactions` — and there is a command that proves it.
* **Employees are never hard-deleted.** Departure is a lifecycle status, so
  attendance, leave and payslip history stays joinable.
* **Money is `NUMERIC(19,4)` end to end**, never a float, and crosses the API
  as a string. A JSON number is a 422.

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2.0 async · asyncpg · PostgreSQL 16 ·
Alembic · Pydantic v2 · Redis · pytest + Testcontainers · Ruff · mypy strict ·
Docker · GitHub Actions · structlog · OpenTelemetry · Prometheus · Argon2 · JWT.

Managed with [uv](https://docs.astral.sh/uv/); `uv.lock` is committed.

## Quick start

```bash
make install                                # deps + .env from .env.example
make migrate                                # apply the schema
make create-admin email=admin@example.com   # prints a generated password once
make seed                                   # demo company, ready to run payroll
make run                                    # http://localhost:8001/docs
```

Docker:

```bash
make docker-up
docker compose exec api python -m scripts.seed
```

Quality gates:

```bash
make check    # ruff + mypy strict
make test     # full suite (needs Docker, or USE_EXISTING_DATABASE=true)
```

## Layout

```
app/
├── main.py · api.py · health.py
├── core/            config · security · money · dates · ids · pagination
│                    exceptions · middleware · dependencies
├── shared/          database · unit_of_work · audit · events · outbox
│                    sequences · cache · observability
└── modules/         each: domain/ application/ infrastructure/ api/
    ├── identity/        users, roles, permissions, refresh tokens
    ├── organization/    companies, departments, designations, locations, holidays
    ├── employees/       employee lifecycle and employment contracts
    ├── salary/          components, structures, the formula registry
    ├── attendance/      shifts, assignments, attendance records
    ├── leave/           types, policies, requests, transactions, balances
    ├── payroll/         periods, runs, payslips, the calculator
    └── reports/         read-only aggregations, JSON and CSV
scripts/            seed, create_admin, rebuild_leave_balances
docs/               architecture, hrm-rules, payroll-rules, api-examples
tests/              unit (no DB) · integration (real PostgreSQL) · api
```

A module is a vertical slice, not a quarter of four horizontal layers:
understanding leave means opening one directory. This is the same layout as the
sibling [`../Core`](../Core) sales and inventory module.

Dependencies point inward. A module's `domain/` imports nothing from the layers
around it, which is why 119 of the 213 tests need no database at all.

## Documentation

| Document | Contents |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | Layering, Unit of Work, multi-company isolation, the full concurrency table, ERP integration |
| [`docs/hrm-rules.md`](docs/hrm-rules.md) | Employee lifecycle graph, contracts, attendance arithmetic, leave rules, self-service |
| [`docs/payroll-rules.md`](docs/payroll-rules.md) | The eight Golden Rules and the mechanism behind each |
| [`docs/api-examples.md`](docs/api-examples.md) | Real captured curl output, end to end |

## How payroll reaches the general ledger

HRM never writes to the general ledger. Approving a run emits a
`PayrollApproved` event carrying **pre-aggregated, balanced journal lines**,
which the Accounting module consumes as a `CreateJournalCommand`. Neither
module imports the other.

```
debits  = gross earnings + employer contributions
credits = deductions + net pay payable + employer contributions payable
```

`GET /api/v1/payroll-runs/{id}/journal-preview` returns the same lines
read-only, so the accounting effect can be reconciled before approval commits.

## Verified state

Everything below was actually executed, not assumed:

* `ruff check` + `ruff format --check` → clean, 129 source files
* `mypy --strict` → clean, 129 source files
* `alembic upgrade head` on a fresh database, single migration
  `0001_initial_schema`; `alembic check` reports no drift
* `pytest` → **213 passed**, 70% coverage, against real PostgreSQL 16
* `python -m scripts.seed` → ran, and ran again idempotently
* `python -m scripts.rebuild_leave_balances --all --verify` →
  `matched_previous=True`
* Live API run: employee created with an allocated number; leave requested
  (Fri→Mon counted as 2 days), approved, balance moved 25 → 23; payroll run
  calculated for 5 employees; journal preview balanced at £35,057.72 both
  sides; approved; recalculation refused with `PAYROLL_ALREADY_APPROVED`;
  paid; attendance amendment refused with `ATTENDANCE_LOCKED`; CSV export and
  rate-limit headers confirmed; outbox events and audit trail verified in
  `psql`

## Deliberate decisions

Flag any of these if you disagree — none is accidental.

**Payroll is exception-based.** An employee is assumed present on every working
day; pay is reduced only by an explicit `ABSENT`, `HALF_DAY` or unpaid-leave
record. The alternative zeroes out everybody's pay when a biometric feed goes
down, and most companies do not record attendance for salaried staff at all. An
overpayment is recoverable; an unexplained zero paycheque is not.

**Salary components are evaluated in `sequence` order**, each seeing the
running totals of everything before it. That makes "5% of gross" mean something
definite and puts HR in control of what "so far" includes, instead of resolving
a dependency graph whose meaning would change as components are added.

**Formulas are registered Python, not expression strings.** A `FORMULA`
component names a calculator in `app/modules/payroll/domain/formulas.py`. An expression
column would mean `eval` — arbitrary code execution driven by a database row —
or a bespoke parser with its own bugs. Neither belongs in something that
decides what people get paid.

**`TERMINATED` and `RESIGNED` are terminal.** A returning employee is a rehire
with a fresh record, which keeps tenure, leave accrual and payroll history
honest.

**The salary-structure exclusion constraint is partial on `ACTIVE`.** A `DRAFT`
structure is a proposal, so HR can prepare next year's revision while the
current open-ended one is still in force.

**An adjustment run may enter an `APPROVED` or `PAID` period.** Refusing every
run into a closed-by-approval period would make Golden Rule 7 impossible to
obey. Only a `CLOSED` period refuses one.

**Holidays are a first-class table**, though the original specification did not
list one. Leave-day counting and payroll working days are both wrong without
it.

**Reading salary requires `payroll:read`, not `employees:read`.** An HR Officer
who maintains contact details has no business seeing what everybody earns.

## Not implemented (deliberate, documented)

* **The outbox relay process.** Events are written transactionally; publishing
  is a separate concern kept outside the HR transaction, so a broker outage can
  never block a leave approval or a payroll run.
  `OutboxRepository.claim_pending` already uses `FOR UPDATE SKIP LOCKED` for
  when a relay is written.
* **Year-end leave carry-forward as a scheduled job.** The arithmetic exists
  and is tested (`carry_forward_days`), and `CARRY_FORWARD` is a transaction
  type; the annual job that applies it is not written.
* **Multi-currency payroll.** A salary structure carries a `currency_code` and
  amounts round to that currency's own scale, but there is no FX conversion or
  revaluation — a run reports in the company's base currency.
* **Per-user company scoping.** Permissions are global and `company_id` is an
  explicit parameter. Adding scoping is additive to
  `deps.get_current_user` plus a `WHERE` clause, not a schema change.
* **A real tax formula.** The shipped `progressive_tax_3_band` is an
  illustrative worked example with hardcoded bands. Replace it with your
  jurisdiction's own registered formula.

## Gotchas

**Ports.** `docker-compose.yml` maps PostgreSQL, Redis and the API to
**5433 / 6380 / 8001**, not the usual 5432 / 6379 / 8000 — this host already
runs PostgreSQL and Redis, and the sibling Accounting stack claims the
conventional ports. Override in `.env` if your host differs.

**`.env` does not exist until `make install` copies `.env.example`.**
`SECRET_KEY` (≥32 chars) and `DATABASE_URL` are required; settings are read at
import time and the app refuses to start without them. That fail-fast is
intentional.

**Money is a string on the wire** — `"base_salary": "4500.55"`. A JSON number
is a 422 everywhere, including from a pre-commit hook.

**`alembic check` needs the filters in `alembic/env.py`.** Autogenerate cannot
round-trip the CHECK constraints that `Enum(native_enum=False)` generates, nor
the raw-SQL exclusion constraints, so both are excluded from comparison. They
are real — confirm with `\d+ employees` in psql.

**Tests share one session between the fixture and the service under test.** A
service rollback expires ORM fixture objects, so capture ids into locals before
any call expected to fail. Not a production concern (one session per request),
but it will bite when writing new tests.
