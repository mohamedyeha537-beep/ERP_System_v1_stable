import sys
from pathlib import Path
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import create_app
from infra.db import get_db
from modules.catalog.service import list_stockable_products
from modules.inventory.service import get_balance
from modules.reporting import queries as report_queries

create_app()
db = next(get_db())
products = list_stockable_products(db)
wid = 1
avg = report_queries.avg_unit_cost_per_product(db)
total = Decimal("0")
lines = []
for p in products:
    bal = get_balance(db, p.id, wid)
    uc = avg.get(p.id)
    val = (bal * uc).quantize(Decimal("0.001")) if uc is not None else Decimal("0")
    total += val
    if p.id == 10:
        lines.append(f"onion bal={bal} cost={uc} val={val}")
lines.append(f"total={total.quantize(Decimal('0.001'))}")
Path("tools/inv_fin_out.txt").write_text("\n".join(lines), encoding="utf-8")
