import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app

create_app()
from infra.db import get_db
from modules.inventory.service import get_balance, reconcile_all_stock_balances, balance_from_movements

gen = get_db()
db = next(gen)
try:
    n = reconcile_all_stock_balances(db)
    db.commit()
    print("reconcile changed", n)
    print("onion balance", get_balance(db, 10, 1))
    print("onion movements", balance_from_movements(db, 10, 1))
finally:
    gen.close()
