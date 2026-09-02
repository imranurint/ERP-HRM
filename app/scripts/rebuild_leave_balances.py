"""Rebuild `leave_balances` from `leave_transactions`.

    python -m app.scripts.rebuild_leave_balances --all --verify
    python -m app.scripts.rebuild_leave_balances --company <uuid>

`leave_balances` is a projection. This script proves it, by discarding it and
replaying every transaction through the *same* domain function the live code
uses (`app.domain.leave.rules.apply_transaction`). If incremental posting and
a from-scratch rebuild ever disagreed, one of them would be wrong — and with
`--verify` the script says which balances changed rather than quietly
overwriting them.

Run it after any bulk import, or on a schedule as an integrity check.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from app.core.logging import configure_logging, get_logger
from app.domain.leave.rules import ZERO_DAYS, LeaveBalanceSnapshot, apply_transaction
from app.domain.shared.enums import AuditAction
from app.infrastructure.database.models.organization import Company
from app.infrastructure.database.session import dispose_engine, session_scope
from app.infrastructure.database.unit_of_work import UnitOfWork

logger = get_logger("rebuild_leave_balances")

BalanceKey = tuple[UUID, UUID, int]


async def rebuild_company(uow: UnitOfWork, company_id: UUID, *, verify: bool) -> dict[str, int]:
    """Replay one company's transactions. Returns a small summary."""
    previous: dict[BalanceKey, LeaveBalanceSnapshot] = {}
    if verify:
        for row in await uow.leave_balances.list_for_company_all_years(company_id):
            previous[(row.employee_id, row.leave_type_id, row.year)] = LeaveBalanceSnapshot(
                opening_balance=Decimal(str(row.opening_balance)),
                earned=Decimal(str(row.earned)),
                used=Decimal(str(row.used)),
                adjustment=Decimal(str(row.adjustment)),
            )

    transactions = await uow.leave_transactions.all_for_company(company_id)

    rebuilt: dict[BalanceKey, LeaveBalanceSnapshot] = {}
    for employee_id, leave_type_id, year, transaction_type, days in transactions:
        key = (employee_id, leave_type_id, year)
        snapshot = rebuilt.get(
            key, LeaveBalanceSnapshot(ZERO_DAYS, ZERO_DAYS, ZERO_DAYS, ZERO_DAYS)
        )
        # The same function the live path uses — that is the whole point.
        rebuilt[key] = apply_transaction(snapshot, transaction_type, days)

    cleared = await uow.leave_balances.clear_company(company_id)
    for (employee_id, leave_type_id, year), snapshot in rebuilt.items():
        await uow.leave_balances.apply_snapshot(
            company_id=company_id,
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            year=year,
            snapshot=snapshot,
        )

    mismatches = 0
    if verify:
        for key in set(previous) | set(rebuilt):
            before = previous.get(key)
            after = rebuilt.get(key)
            if before != after:
                mismatches += 1
                logger.warning(
                    "leave_balance.mismatch",
                    employee_id=str(key[0]),
                    leave_type_id=str(key[1]),
                    year=key[2],
                    before=str(before.closing_balance) if before else None,
                    after=str(after.closing_balance) if after else None,
                )

    uow.audit.record(
        action=AuditAction.LEAVE_BALANCES_REBUILT,
        entity_type="LeaveBalance",
        entity_id=str(company_id),
        company_id=company_id,
        new_data={
            "transactions_replayed": len(transactions),
            "balances_written": len(rebuilt),
            "balances_cleared": cleared,
            "mismatches": mismatches,
        },
    )
    await uow.commit()

    return {
        "transactions": len(transactions),
        "balances": len(rebuilt),
        "cleared": cleared,
        "mismatches": mismatches,
    }


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild leave_balances from leave_transactions.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Rebuild every company.")
    group.add_argument("--company", help="Rebuild one company by id.")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Compare the rebuilt balances against the existing ones and report differences.",
    )
    args = parser.parse_args(argv)

    configure_logging()
    exit_code = 0

    try:
        async with session_scope() as session:
            uow = UnitOfWork(session)

            if args.all:
                company_ids = list((await session.execute(select(Company.id))).scalars().all())
            else:
                company_ids = [UUID(args.company)]

            total_mismatches = 0
            for company_id in company_ids:
                summary = await rebuild_company(uow, company_id, verify=args.verify)
                total_mismatches += summary["mismatches"]
                print(
                    f"{company_id}: replayed {summary['transactions']} transactions "
                    f"into {summary['balances']} balances "
                    f"(cleared {summary['cleared']}, mismatches {summary['mismatches']})"
                )

            if args.verify:
                matched = total_mismatches == 0
                print(f"matched_previous={matched}")
                # A non-zero exit makes this usable as a scheduled integrity
                # check that actually fails a pipeline when it should.
                exit_code = 0 if matched else 2
    finally:
        await dispose_engine()

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
