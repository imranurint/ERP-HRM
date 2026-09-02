# Architecture

## Why a modular monolith

An HR action and its consequences must commit together or not at all.
Approving a payroll run writes payslips, payslip items, run totals, a period
status change, an audit row and an outbox event — six tables. Split those
across services and you are reconciling them forever; keep them in one
database transaction and the question never arises.

So this is a **modular monolith**, the same decision the sibling
[`../Accounting`](../../Accounting) general ledger made, for the same reason.
Modules are separated by *code structure and dependency direction*, not by
network boundaries. If a module ever genuinely needs to be extracted, the
seams are already drawn along the domain boundaries below.

## Layers

Dependencies point inward. Nothing in an inner layer imports from an outer one.

```
app/api/v1/          HTTP. Authenticate, authorise, parse, delegate, return.
                     Zero business logic.
        ↓
app/application/     Use-case services, schemas, commands.
                     Owns transaction boundaries.
        ↓
app/domain/          Entities, enums, rules, events.
                     Pure Python. No SQLAlchemy, no FastAPI, no I/O.
        ↑
app/infrastructure/  ORM models, repositories, Unit of Work, cache, outbox,
                     observability. Implements what the layers above need.
```

`app/domain/` is the part that matters most. Every invariant — the employment
status graph, leave day counting, the payslip calculator, the payroll Golden
Rules — is a pure function over plain values. That is what makes them testable
without a database (119 of the 213 tests need no PostgreSQL at all) and
reusable from a bulk import or a scheduled job that never goes near HTTP.

### Domain boundaries

| Module | Owns |
| --- | --- |
| `domain/identity` | Users, roles, permissions, the authenticated caller |
| `domain/organization` | Companies, departments, designations, locations |
| `domain/employees` | Employee lifecycle, contracts, hierarchies |
| `domain/attendance` | Shifts, check-in/out arithmetic, day classification |
| `domain/leave` | Leave types, policies, day counting, balance arithmetic |
| `domain/payroll` | Salary structures, the calculator, formulas, Golden Rules |
| `domain/shared` | Cross-cutting enums and the domain event catalogue |

## Repository pattern and Unit of Work

Repositories own SQL. Application services own orchestration. Nothing above
`app/infrastructure/` imports SQLAlchemy, which is what keeps the domain
testable and lets the persistence strategy change without rewriting the rules.

**One UoW == one database transaction == one HR operation.** A payroll run
opens a UoW and does everything inside it:

```
lock the period (SELECT ... FOR UPDATE)
  → lock the run
    → check the Golden Rule guard
      → resolve employees, salary structures, attendance, leave (bulk queries)
        → compute every payslip
          → insert payslips + items
            → update run totals
              → write the audit row
                → enqueue the outbox event
                  → single COMMIT
```

Any failure rolls the whole thing back. There is no code path that can leave
half a payroll behind. Repositories never commit; only the UoW decides.

## Multi-company strategy

`company_id` is on every business table and is the isolation boundary.

Three things enforce it, and all three are needed:

1. **Every unique constraint is scoped by company.** Two companies may both
   have a department coded `ENG`.
2. **Every repository lookup that takes a caller-supplied id is `get_scoped`**
   — it filters by id *and* company in one query. Fetching by id and checking
   the company afterwards is the mistake that leaks one tenant into another's
   response, so the pattern removes the opportunity.
3. **`company_id` is an explicit parameter** on every service method and an
   explicit query parameter on every route. It is never inferred.

Permissions are currently global; company scoping is by parameter. Adding
per-user company scoping is additive — a `WHERE` clause in
`deps.get_current_user` plus a check in `ensure_self_or_permission`, not a
schema change.

## Concurrency

Isolation level is **READ COMMITTED**, PostgreSQL's default. Correctness comes
from explicit mechanisms rather than a blanket SERIALIZABLE, which would
serialise unrelated payroll runs and buy nothing the locks do not already give.

| Hazard | Mechanism |
| --- | --- |
| Two payroll runs on one period | `SELECT ... FOR UPDATE` on `payroll_runs` and `payroll_periods`, plus a live-run check |
| Period closed mid-run | `SELECT ... FOR UPDATE` on the period row |
| Two approvers on one leave request | `SELECT ... FOR UPDATE` on `leave_requests` |
| Lost leave-balance updates | `INSERT ... ON CONFLICT DO UPDATE SET used = used + :delta` — never read-modify-write |
| Duplicate employee/contract/run numbers | Single atomic `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` on `document_sequences` — never `SELECT MAX()` |
| Overlapping payroll periods | GiST exclusion constraint on `(company_id, daterange(...))` |
| Overlapping salary structures | GiST exclusion constraint, **partial on `status = 'ACTIVE'`** |
| Overlapping shift / leave-policy assignments | GiST exclusion constraints on `(employee_id, daterange(...))` |
| Two active contracts for one employee | Partial unique index on `(employee_id) WHERE status = 'ACTIVE'` |
| Concurrent employee or leave edits | `version` column, optimistic lock |
| Duplicate attendance for a day | Unique `(employee_id, attendance_date)` |
| Outbox relay races | `SELECT ... FOR UPDATE SKIP LOCKED` |

The application also checks most of these before touching the database. That
is deliberate duplication: the application check produces a good error message
in the common case, and the constraint is what actually holds when two
requests arrive at once and both pass the check.

## Payroll correctness

Four properties, each with a mechanism behind it:

**Deterministic.** `app/domain/payroll/calculator.py` is a pure function.
Given the same `PayrollInput` it produces byte-identical output on any machine,
forever. No clock, no randomness, no configuration lookups, no database.

**Exact.** Money is `Decimal` and `NUMERIC(19,4)` end to end. There is no float
anywhere in the payroll path, a pre-commit hook rejects one being introduced,
and money crosses the API as a *string* — a JSON number is a 422, because most
clients parse JSON numbers as IEEE-754 doubles and would round a salary before
validation ever ran.

**Explainable.** Every payslip stores a `calculation_snapshot`: contractual
salary, proration factor, working and payable days, component evaluation
order. Six months later, "why was March short?" is answered from that column
and does not depend on the employee's *current* configuration.

**Immutable once approved.** See [`payroll-rules.md`](payroll-rules.md).

### Evaluation order

Salary components are evaluated in `sequence` order (ties broken by
`component_code`, so the order is total and stable), and each component sees
the running totals of everything before it. That makes "pension is 5% of
gross" unambiguous — it means 5% of the gross accumulated so far, and HR
controls what "so far" includes.

The alternative, resolving a dependency graph, would make the same
configuration mean different things depending on which components happened to
exist. This way the answer is always visible in the sequence numbers.

### Formulas are code, not data

A `FORMULA` component names a calculator registered in
`app/domain/payroll/formulas.py`. It does **not** carry an expression string.

An expression column would mean either `eval` — arbitrary code execution driven
by a database row — or a bespoke mini-language with its own parser, its own
bugs and no type checking. Neither is acceptable for something that decides
what people get paid. A registered formula is ordinary Python: reviewed,
unit-tested, type-checked and versioned with the rest of the codebase. Adding a
jurisdiction's tax bands is a pull request, which is the right amount of
ceremony for that change.

## Auditability

Three separate records, because they answer different questions:

| Table | Question it answers | Shape |
| --- | --- | --- |
| `audit_logs` | Who changed which row, from what to what? | Append-only, `previous_data` / `new_data` JSONB |
| `employee_lifecycle_events` | What happened to this person's employment? | Append-only, business-level events |
| `leave_transactions` | Why is this balance what it is? | Append-only ledger the balance projection is derived from |

Audit rows share the transaction of the operation they describe, so an audit
row can never survive an operation that rolled back, and an operation can never
commit without its audit row.

Credential-shaped keys are stripped before anything is persisted
(`repositories/audit.scrub`) and again before anything is logged
(`core/logging._redact_processor`). Personal fields — date of birth, personal
contact details, national id, bank account — are in the log redaction list too.

**Employees are never hard-deleted.** Departure is an `employment_status`, so
every attendance record, leave transaction and payslip the person generated
stays joinable and reportable.

## Derived data is rebuildable

`leave_balances` is a projection. `leave_transactions` is the append-only truth
it derives from, and both the live path and the rebuild share one function
(`domain.leave.rules.apply_transaction`) so they cannot drift.

```bash
make rebuild-leave-balances     # replays everything, reports matched_previous
```

It exits non-zero on a mismatch, so it works as a scheduled integrity check.
This is the same relationship the Accounting module has between its journals
and `ledger_balances`, for the same reason: a derived table you cannot rebuild
is a derived table you cannot trust.

## Future ERP integration

### How payroll reaches the general ledger

HRM does not know what a journal entry is, and it never writes to the general
ledger. What it knows is which GL account code each salary component maps to,
because that mapping is HR configuration.

Approving a payroll run emits a `PayrollApproved` domain event carrying
**pre-aggregated, balanced journal lines**:

```
debits  = gross earnings + employer contributions
credits = deductions + net pay payable + employer contributions payable
        = deductions + (gross − deductions) + employer
        = gross + employer                                    ✓ balanced
```

The Accounting module consumes that payload and builds a
`CreateJournalCommand` — its own stable contract. Neither module imports the
other. `GET /api/v1/payroll-runs/{id}/journal-preview` returns exactly the
same lines read-only, so the accounting effect can be reconciled *before*
approval commits.

The two payable account codes are configuration
(`PAYROLL_NET_PAYABLE_ACCOUNT`, `PAYROLL_EMPLOYER_LIABILITY_ACCOUNT`), and
`gl_account_code` on a salary component is a plain string rather than a foreign
key — HRM must not depend on another service's chart-of-accounts schema.

### The transactional outbox

Events are written to `outbox_events` in the same transaction as the change
they describe, so either both land or neither does. Publishing to a bus is a
separate concern deliberately kept outside the HR transaction: a broker outage
must never block a leave approval or a payroll run.

Published events: `EmployeeHired`, `EmployeeStatusChanged`,
`EmployeeTransferred`, `SalaryChanged`, `LeaveRequested`, `LeaveApproved`,
`LeaveRejected`, `LeaveCancelled`, `PayrollCalculated`, `PayrollApproved`,
`PayrollPaid`.

**The relay process itself is not implemented** — see the README's scope
section. `OutboxRepository.claim_pending` uses `FOR UPDATE SKIP LOCKED` so
several relay workers can run concurrently when one is written.

### Modules this is built to serve

Recruitment (feeds `EmployeeHired`), Projects and Asset Management (consume the
employee directory), Procurement and CRM (consume org structure), Accounting
(consumes `PayrollApproved`). Each integrates through domain events and the
read APIs, not by reaching into these tables.

## Identifiers

UUIDv7 (`app/core/ids.py`), generated by the application. Two reasons:

- **Time-ordered**, so index inserts stay local under load rather than
  scattering across the B-tree the way UUIDv4 does.
- **Client-side generation** lets a whole object graph — a run, its payslips,
  their items, the outbox event — be built in memory before touching the
  database, keeping the payroll transaction to a bounded number of round-trips.

Because ids are time-ordered, cursor pagination is just `WHERE id > :cursor
ORDER BY id`: correct under concurrent inserts and index-friendly.

## Timezones

Everything stored is UTC and timezone-aware. Everything the domain *reasons*
about — which day a shift belongs to, whether leave has started — is a local
business date in the company's (or location's) timezone.

`core/dates.business_today()` exists so that `date.today()` never appears in
business logic. A server in UTC would tell a Manila office it is still
yesterday for eight hours a day, and every "has this started?" question would
be wrong for all of them.

## Testing strategy

- **Unit** (119 tests): pure domain rules. No database, fast, run first in CI.
- **Integration** (51 tests): real PostgreSQL 16. The schema uses GiST
  exclusion constraints, partial unique indexes, `ON CONFLICT DO UPDATE` and
  `SELECT ... FOR UPDATE` — none of which SQLite can emulate, so testing
  against it would test nothing. Several tests insert *past* the services on
  purpose, to prove the database itself holds the line.
- **API** (43 tests): routing, serialisation, authorisation, error envelope.

Isolation is by `TRUNCATE`, not by an outer transaction that never commits —
the services commit, and wrapping them would hide exactly the behaviour under
test.
