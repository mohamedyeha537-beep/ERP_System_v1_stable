"""One-off: reconcile balances and print onion state."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import sessionmaker

from infra.db import get_engine
import modules.authz.models  # noqa: F401
import modules.catalog.models  # noqa: F401
import modules.inventory.models  # noqa: F401
import modules.payments.models  # noqa: F401
import modules.sales.models  # noqa: F401

from modules.inventory.service import (
    balance_from_movements,
    get_balance,
    reconcile_all_stock_balances,
)
from modules.reporting import queries as rq

Session = sessionmaker(bind=get_engine())
with Session() as db:
    n = reconcile_all_stock_balances(db)
    db.commit()
    pid = 10
    print("reconciled rows:", n)
    print("balance table:", get_balance(db, pid, 1))
    print("from movements:", balance_from_movements(db, pid, 1))
    costs = rq.avg_unit_cost_per_product(db)
    cost = costs.get(pid)
    bal = get_balance(db, pid, 1)
    print("unit cost:", cost, "value:", (bal * cost).quantize("0.001") if cost else None)
