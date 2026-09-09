#!/usr/bin/env python3
"""Print estimated API spend and remaining budget.

Reads the local usage ledger (COSTS.md). Figures are estimates derived from
metered usage, not from a billing API.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.ai.budget_guard import BudgetGuard  # noqa: E402
from app.ai.usage import get_usage_ledger  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.db.repositories import total_estimated_cost  # noqa: E402


async def _carried_forward() -> object:
    """Spend recorded by previous runs. Reports zero if there is no database."""
    settings = get_settings()
    if not settings.persistence_enabled:
        return None
    database = Database.from_settings(settings)
    try:
        await database.create_schema()
        async with database.session() as db:
            return await total_estimated_cost(db)
    except Exception as exc:  # a missing database is not an error to report on
        print(f"  (could not read persisted usage: {exc})")
        return None
    finally:
        await database.dispose()


def main() -> int:
    settings = get_settings()
    ledger = get_usage_ledger()

    carried = asyncio.run(_carried_forward())
    if carried is not None:
        ledger.set_baseline(carried)  # type: ignore[arg-type]

    guard = BudgetGuard(settings, ledger)
    summary = guard.summary()

    print("CareLine AI - estimated API spend")
    print("-" * 40)
    print(f"  Project total   ${float(summary['estimated_project_cost_usd']):>7.2f}")
    print(f"  Warn threshold  ${float(summary['warn_threshold_usd']):>7.2f}")
    print(f"  Ceiling         ${float(summary['project_limit_usd']):>7.2f}")
    print(f"  Remaining       ${float(summary['remaining_usd']):>7.2f}")
    print(f"  Status          {summary['status']}")
    print(f"  Paid calls      {'allowed' if summary['paid_calls_allowed'] else 'BLOCKED'}")
    by_provider = summary["by_provider"]
    if isinstance(by_provider, dict) and by_provider:
        print("  By provider:")
        for provider, cost in sorted(by_provider.items()):
            print(f"    {provider:<12} ${float(cost):>7.2f}")
    else:
        print("  No provider usage recorded (mock providers cost nothing).")

    if settings.can_spend_money:
        active = ", ".join(sorted(settings.selected_paid_providers))
        print(f"\n  NOTE: paid providers active: {active}")
    else:
        print("\n  No paid providers are active in this configuration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
