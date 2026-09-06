# HRM rules

Employee lifecycle, attendance and leave. Payroll has its own document:
[`payroll-rules.md`](payroll-rules.md).

Every rule here is an executable guard in a module's `domain/`, and every mutation
calls one.

## Employee lifecycle

### Employees are never deleted

Departure is an `employment_status`, never a `DELETE`. Every attendance record,
leave transaction and payslip the person ever generated stays joinable and
reportable — a legal requirement in most jurisdictions and an operational one
everywhere.

### The status graph

```
                 ┌──────────────┐
                 │  PROBATION   │
                 └──┬────────┬──┘
                    │        │
              ┌─────▼──┐     │
      ┌───────►  ACTIVE ◄────┼──────┐
      │       └──┬──┬──┬─────┘      │
      │          │  │  │            │
┌─────┴────┐  ┌──▼──┴──▼───┐   ┌────┴─────┐
│ ON_LEAVE │  │ SUSPENDED  │   │ INACTIVE │
└──────────┘  └────────────┘   └──────────┘
                    │
        ┌───────────┴────────────┐
        ▼                        ▼
  ┌────────────┐          ┌────────────┐
  │ TERMINATED │          │  RESIGNED  │   ← terminal, no way back
  └────────────┘          └────────────┘
```

`ALLOWED_STATUS_TRANSITIONS` in
[`app/modules/employees/domain/rules.py`](../app/modules/employees/domain/rules.py) is the
authority. Anything not listed is rejected.

**`TERMINATED` and `RESIGNED` are terminal.** A returning employee is a
*rehire* and gets a fresh employment record rather than resurrecting a closed
one. That keeps tenure, leave accrual and payroll history honest — a person who
left for three years should not silently accrue seniority across the gap.

The two are distinct because they are legally and statistically different, and
reports separate them. `POST /employees/{id}/terminate` takes `resigned: true`
for a voluntary departure.

### Status invariants the database enforces

- `is_active` is a projection of `employment_status`, and a `CHECK` constraint
  stops the two drifting apart under a partial update. Headcount reports depend
  on that flag.
- A terminal status **requires** a `termination_date`. Without one, "when did
  they leave?" is unanswerable and tenure and final-pay reporting break.
- `date_of_birth < hire_date`, and `termination_date >= hire_date`.

### Material changes leave a trail

`PATCH /employees/{id}` handles contact and identity details only. Department,
designation and manager changes go through `/transfer` and `/promote`, and
status changes through the lifecycle routes, so every material change writes an
`employee_lifecycle_events` row with before-and-after data.

`PATCH` takes an optional `version` for optimistic locking — send the version
you read, and a concurrent edit gives you a 409 instead of silently
overwriting.

Lifecycle transitions take a `SELECT ... FOR UPDATE` on the employee row first.
They are read-then-write, so without the lock two concurrent terminations would
both pass the transition check and produce two lifecycle events for one
departure.

### Hierarchies

Both the reporting line and the department tree are cycle-checked. The service
walks the proposed parent's ancestors; if the subject appears among them the
assignment is rejected. PostgreSQL cannot express "acyclic" as a constraint, so
this guard has to live in the service that does the walking — and without it,
every org-chart traversal after the bad edit would never terminate.

A departed employee cannot manage anybody.

## Employment contracts

**At most one `ACTIVE` contract per employee**, enforced by a partial unique
index on `(employee_id) WHERE status = 'ACTIVE'` rather than an application
check. Two concurrent activations would both pass an application check and
leave the employee with two contradictory contracts — and payroll would then
have to guess which one to pay against.

An employee may accumulate any number of `DRAFT`, `EXPIRED` and `TERMINATED`
contracts over a career; only one can be in force.

Every contract type except `PERMANENT` **must** carry an end date. Without one
a fixed-term contract can never expire, and the database refuses it.

**Expiry is derived, not scheduled.** `contract_status_on` reports a contract
past its end date as `EXPIRED` on read, even if no batch job has run. A missed
cron job degrades reporting; it must never let payroll pay against a lapsed
contract.

## Attendance

### Which day does a check-in belong to?

The instant is stored in UTC. The `attendance_date` it is filed under is the
calendar date in the **company's** timezone — or the **location's**, if that
site has its own. An 01:00 UTC check-in belongs to a different day in Manila
than in London, and getting it wrong misfiles a whole office's attendance.

### Overnight shifts

A shift whose `end_time` is at or before its `start_time` (22:00 → 06:00)
finishes on the following calendar day. The record is still filed under the day
the shift **started**, so a night worker's Monday shift is one attendance row,
not two halves. Checking out after midnight closes the previous day's open
record.

### Derived figures

All integer minute arithmetic — no floats, no timedeltas leaking into storage.

| Figure | Rule |
| --- | --- |
| `working_minutes` | Whole minutes between check-in and check-out, floored. Partial minutes are never rounded up into paid time. |
| `late_minutes` | Zero inside the grace window, then the **whole** delay. Grace exists so a two-minute delay never becomes a payroll deduction. |
| `early_leave_minutes` | Minutes short of the scheduled end. |
| `overtime_minutes` | Minutes beyond the shift, past a configurable threshold. |

Status is derived: `HALF_DAY` when less than half the shift was worked,
otherwise `LATE` if late, otherwise `PRESENT`. **Half-day outranks late** —
somebody who worked two hours of an eight-hour shift has a bigger problem than
the fifteen minutes they arrived late, and proration cares about the former.

On a weekend or holiday, lateness and early-leave are meaningless and anything
worked is overtime in full.

### The figures are stored, not recomputed

Every derived minute count is computed at capture time and written to the row.
Amending a shift definition next year therefore cannot silently rewrite last
year's attendance.

### Provenance

`source` (`MANUAL` / `BIOMETRIC` / `API` / `IMPORT`) is never overwritten. A
record from a biometric terminal and one keyed in by an HR officer carry
different weight in a dispute.

A repeat check-in on the same day is **not an error** — a second badge swipe is
a normal human action, and the existing record is returned.

### Locking

Attendance inside an `APPROVED`, `PAID` or `CLOSED` payroll period cannot be
created or amended. The payslips derived from it have already been signed off.

## Leave

### Day counting

Weekends and public holidays are excluded by default: booking Friday to Monday
over a normal weekend uses **two** days of entitlement, not four. A leave type
can set `excludes_non_working_days: false` for schemes that consume calendar
days.

Days are `Decimal` with two places, because half days are real and
`0.5 + 0.5 + 0.5` must be exactly `1.5`.

### Only approved leave consumes balance

A `PENDING` request is *checked* against the balance but writes no transaction,
so rejecting one needs no unwinding. The reservation is enforced by counting
open requests:

```
available = closing_balance − days held by DRAFT/PENDING requests
```

That is what stops two requests submitted the same morning both being approved
against one remaining day.

### The balance is a projection

`leave_transactions` is append-only truth; `leave_balances` is derived from it.

```
closing_balance = opening_balance + earned − used + adjustment
```

A `CHECK` constraint verifies that identity on every write. The projection is
updated by an atomic `INSERT ... ON CONFLICT DO UPDATE` — never
read-modify-write, which would lose updates when two approvals touch the same
employee and leave type at once.

| Transaction | Column | Meaning |
| --- | --- | --- |
| `ENTITLEMENT`, `ACCRUAL` | `earned` + | Days granted |
| `CARRY_FORWARD` | `opening_balance` + | Brought in from last year |
| `CONSUMPTION` | `used` + | Leave approved |
| `CANCELLATION` | `used` − | Approved leave cancelled |
| `ADJUSTMENT`, `EXPIRY` | `adjustment` ± | Manual correction, signed |

Cancellation reduces `used` rather than inflating `earned`, because the
employee never earned those days twice.

`make rebuild-leave-balances` replays every transaction through the *same*
function the live path uses and reports whether the result matches.

### Approval

- **Nobody approves their own leave**, whatever permissions they hold. An HR
  Manager who is also an employee still cannot sign off their own holiday: a
  permission grant is not supposed to be a loophole.
- The request row is locked (`SELECT ... FOR UPDATE`) before its status is
  read, so two approvers cannot both win.
- The balance is re-checked **at approval time**, not only at request time —
  days may have been consumed in between.
- A leave type with `requires_approval: false` is approved on submission.
  Otherwise it would sit `PENDING` forever with nobody to action it.

### Cancellation

Allowed until the leave has started. Approved leave that has **already begun**
cannot be cancelled: doing so would silently return days the employee actually
took. The sanctioned remedy is an explicit balance adjustment, which requires a
written reason and leaves an audit trail.

### Overlap

A new request may not overlap any `DRAFT`, `PENDING` or `APPROVED` leave the
employee already holds. Rejected and cancelled leave no longer blocks anything.

## Self-service

An employee's login is linked to their employee record (`employees.user_id`,
partial-unique). `ensure_self_or_permission` is the boundary:

- pointing a route at **yourself** always works;
- pointing it at **anybody else** requires the corresponding permission.

So an `Employee`-role caller holds `leave:read` and may read their own balance;
an HR Officer holds `employees:read` and may read anyone's in the company. The
`Employee` role deliberately holds almost nothing else.

Reading salary needs `payroll:read`, not `employees:read` — an HR Officer who
maintains contact details has no business seeing what everybody earns.

## Audit requirements

Audited at minimum: employee create and update, every lifecycle transition,
salary structure creation and activation, attendance recording and amendment,
leave request/approve/reject/cancel, balance adjustments, payroll
calculate/approve/pay, payslip voiding, user creation, role changes, login and
logout.

Never written to `audit_logs` or the application log: passwords, password
hashes, JWTs, refresh tokens, API keys, private keys. Personal fields (date of
birth, personal email and phone, national id, bank account) are additionally
redacted from logs.
