# Payroll rules

Every rule here has an executable guard in
[`app/domain/payroll/rules.py`](../app/domain/payroll/rules.py), and every
payroll mutation calls one. This document explains *why*; the code is the
authority on *what*.

## The Golden Rules

### Rule 1 — payroll periods cannot overlap

Enforced twice:

- `ensure_no_period_overlap` produces a good error message in the normal case;
- a **GiST exclusion constraint** on `(company_id, daterange(start_date,
  end_date, '[]'))` is what actually holds.

Both are needed. Two simultaneous inserts both pass the application check and
both commit; the constraint cannot be raced. If an overlap were possible, a day
of attendance would belong to two periods and could be paid twice.

### Rule 2 — approved payroll cannot be modified

`ensure_run_mutable` refuses recalculation, payslip deletion and any edit once
a run is `APPROVED`. A human has signed the numbers off.

### Rule 3 — paid payroll cannot be modified

Same guard, stronger case: the money has left the building. In most
deployments the figures have already gone to a bank and a tax authority.
Editing them in place would break reconciliation with no trace.

### Rule 4 — payroll calculations must be deterministic

`app/domain/payroll/calculator.py` is a pure function. No clock, no
randomness, no configuration lookups, no I/O. The same `PayrollInput` produces
byte-identical output on any machine, forever.

This is what makes recalculating a two-year-old period a safe thing to do, and
it is why registered formulas must also be pure functions of their
`FormulaContext`.

### Rule 5 — salary values must use Decimal

`Decimal` in Python, `NUMERIC(19,4)` in PostgreSQL, **string on the wire**.

A JSON number is a 422. Most clients parse JSON numbers as IEEE-754 doubles,
so accepting `4500.55` as a number would let a rounding error into a salary
before validation ever ran. A pre-commit hook rejects `float(` applied to
anything money-shaped.

Rounding happens **once per line**, at the currency's own scale
(`decimal_places` — JPY has none, KWD has three). Totals are sums of
already-rounded lines, so a payslip's parts always add up to its totals exactly
— a `CHECK` constraint on `payslips` verifies `net = gross − deductions` on
every write.

### Rule 6 — every payslip stores its calculation snapshot

`payslips.calculation_snapshot` (JSONB) records the complete set of inputs:
contractual base salary, whether it was prorated and by what factor, working
days, payable days, unpaid leave days, overtime minutes, the component
evaluation order, taxable earnings, and the period and run it belonged to.

Each `payslip_items` row additionally stores its own `calculation_metadata` —
the percentage and base used, or the formula key and its inputs.

Six months later, "why was March short?" is answered from those columns and
does not depend on the employee's *current* configuration. For the same reason
`component_name` and `component_type` are **copied onto the payslip item, not
joined**: renaming a salary component next year must not rewrite the wording of
a payslip already issued. A payslip is a document, and documents do not change
retroactively.

### Rule 7 — post-approval changes need a reversal or adjustment

Never edit history. A correction is a **new run** with `adjusts_run_id` set to
the original. Both stay visible forever.

`ensure_period_accepts_adjustment` exists specifically so this is possible: an
ordinary run needs an `OPEN` period, but an *adjustment* run may enter a period
that approval already moved to `APPROVED` or `PAID`. Only a `CLOSED` period
refuses one, and reopening it is a deliberate act.

Voiding a payslip is the smaller version of the same idea: the row stays, gains
a `void_reason`, and drops out of every total. Voiding a **paid** payslip is
refused outright.

### Rule 8 — payroll runs inside controlled transactions

One Unit of Work, one database transaction, one payroll operation. Every state
transition takes two row locks first:

```
SELECT ... FOR UPDATE on payroll_runs     -- two operators cannot both approve
SELECT ... FOR UPDATE on payroll_periods  -- nobody can close it mid-run
```

If calculation raises, the whole transaction rolls back and a *separate*
transaction records the failure on the run, so it never sits in `PROCESSING`
with nothing explaining why.

Rule 8 also runs the other way: once a period is `APPROVED`, `PAID` or
`CLOSED`, the attendance and leave underneath it are frozen
(`ensure_period_unlocked_for_source_data`). Editing the inputs of a signed-off
payslip would invalidate it silently.

## Salary structures

An employee's salary structures **must not overlap in time** — otherwise "what
was their salary on 2026-03-01?" has two answers and payroll picks one
arbitrarily.

The GiST exclusion constraint is **partial, on `status = 'ACTIVE'`**. A `DRAFT`
structure is a proposal, not a fact: HR must be able to prepare next year's
revision while the current open-ended structure is still in force. Only
`ACTIVE` rows answer the question, so only `ACTIVE` rows are mutually
exclusive.

Activating a structure writes a lifecycle event and a `SalaryChanged` domain
event alongside the row. Pay is the thing people query most often and dispute
most bitterly.

## Salary components

| Type | Affects gross | Affects net | Meaning |
| --- | --- | --- | --- |
| `EARNING` | yes | +1 | Pay the employee receives |
| `DEDUCTION` | no | −1 | Withheld from the employee |
| `EMPLOYER_CONTRIBUTION` | no | 0 | Costs the employer, never the employee's money |

Employer contributions are a distinct type precisely so they cannot be quietly
folded into either total.

| Calculation | Needs | Notes |
| --- | --- | --- |
| `FIXED` | `amount` | |
| `PERCENTAGE` | `percentage` + `percentage_base` | Base is explicit: `BASE_SALARY`, `GROSS_EARNINGS` or `TAXABLE_EARNINGS` |
| `FORMULA` | `formula_key` | Names a registered calculator; never an expression string |

`percentage_base` is explicit because "10% pension" means something quite
different against base salary than against gross earnings, and getting it wrong
is a silent, recurring error.

A `PERCENTAGE` of `BASE_SALARY` uses the **contractual** salary, not the
prorated basic: a 15% allowance should not shrink because somebody took a day
off.

Component configuration is validated **when the component is saved**, not when
payroll runs — including resolving the formula key. A bad component must not
blow up mid-run, when half the payslips already exist.

## Proration

```
proration_factor = payable_days / working_days     clamped to [0, 1]
basic            = base_salary × factor            (if prorate_base_salary)
```

Clamping matters: attendance data claiming more payable days than the month
contains is a bug upstream, and paying 110% of salary is a worse failure than
paying 100%.

`prorate_base_salary` is a per-structure flag, not a global setting. A
monthly-salaried manager and an hourly-paid operator are both employees, and
only one of them should lose pay for a short day.

### Payroll is exception-based

An employee is assumed **present** on every working day in the period. Pay is
reduced only by an *explicit* record saying otherwise: an `ABSENT` day, a
`HALF_DAY`, or a day of unpaid leave.

The alternative — assume absent unless proven present — is wrong for the common
case. Most companies do not record attendance for salaried staff at all, and
under that model a missing biometric feed would zero out everybody's pay. Under
this model a missing feed pays people normally, which is the safe failure
direction: an overpayment is recoverable, an unexplained zero paycheque is not.

Working days exclude weekends and holidays, and are clipped to the period the
employee was actually employed for — a mid-month starter is paid from their
start date, a leaver to their last day.

Paid leave deducts nothing; that is what "paid" means. Only unpaid leave
reduces payable days.

## Negative net pay is refused

Deductions exceeding earnings raises `NEGATIVE_NET_PAY`. It is not clamped to
zero, because a negative net is always a configuration error and silently
paying zero would hide it for months.

## Run and payslip state machines

```
Run:      DRAFT ──calculate──► PROCESSING ──► COMPLETED ──approve──► APPROVED ──pay──► PAID
                                    │                                                    
                                    └──error──► FAILED ──(fix and re-run)──► PROCESSING

Payslip:  DRAFT ──► CALCULATED ──► APPROVED ──► PAID
                          └──► VOID  (not from PAID)

Period:   OPEN ──► PROCESSING ──► APPROVED ──► PAID ──► CLOSED
```

`FAILED` is mutable on purpose: a run that blew up halfway is meant to be fixed
and re-run, not abandoned.

## What a deployment is expected to change

The shipped formulas in `app/domain/payroll/formulas.py` are illustrative but
real — overtime at time-and-a-half, absence deduction at the daily rate, and a
three-band progressive tax. **Replace the tax formula with your jurisdiction's
own registered formula.** The bands are hardcoded deliberately: real tax codes
differ per country and are not something to express in configuration rows.

`GET /api/v1/salary-components/formulas` lists what is registered, so an
administrator picks a valid key rather than discovering the mistake at payroll
time.
