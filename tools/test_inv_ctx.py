import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decimal import Decimal

from sqlalchemy.orm import Session

from infra.db import get_db
from app.main import create_app
from modules.catalog.service import list_stockable_products
from modules.inventory.service import balance_from_movements, get_balance
from modules.reporting import queries as report_queries

app = create_app()
gen = get_db()
db: Session = next(gen)
try:
    products = list_stockable_products(db)
    avg = report_queries.avg_unit_cost_per_product(db)
    print("avg count", len(avg))
    print("onion avg", avg.get(10))
    pid = 10
    print("get_balance", get_balance(db, pid, 1))
    print("from_movements", balance_from_movements(db, pid, 1))
    for p in products[:3]:
        print(p.id, p.name_ar, "cost", avg.get(p.id), "bal", get_balance(db, p.id, 1))
finally:
    gen.close()
