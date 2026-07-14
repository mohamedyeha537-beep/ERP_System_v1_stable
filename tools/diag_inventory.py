"""Diagnose inventory balance vs ledger for a product."""
from decimal import Decimal
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from modules.catalog.models import Product
from modules.inventory.models import StockBalance, StockMovement
from modules.inventory.service import get_balance, reconcile_all_stock_balances
from modules.payments.models import PurchaseLine
from modules.reporting import queries as report_queries

engine = create_engine("sqlite:///pos.db")

with Session(engine) as db:
    products = db.execute(
        select(Product.id, Product.name_ar).where(Product.name_ar.contains("بصل"))
    ).all()
    print("Onion products:", products)
    if not products:
        products = db.execute(select(Product.id, Product.name_ar).limit(5)).all()
        print("First 5 products:", products)

    pid = products[0][0] if products else None
    if pid is None:
        raise SystemExit("no product")

    print("\n=== Product", pid, products[0][1], "===")
    balances = db.execute(
        select(StockBalance.warehouse_id, StockBalance.quantity).where(
            StockBalance.product_id == pid
        )
    ).all()
    print("stock_balances:", balances)

    mov_sums = db.execute(
        select(StockMovement.warehouse_id, func.sum(StockMovement.quantity), func.count())
        .where(StockMovement.product_id == pid)
        .group_by(StockMovement.warehouse_id)
    ).all()
    print("movement sums by warehouse:", mov_sums)
    total_mov = db.execute(
        select(func.sum(StockMovement.quantity)).where(StockMovement.product_id == pid)
    ).scalar()
    print("total movement sum:", total_mov)

    wh1_bal = get_balance(db, pid, 1)
    print("get_balance wh=1:", wh1_bal)

  # purchase lines
    pl = db.execute(
        select(PurchaseLine).where(PurchaseLine.product_id == pid)
    ).scalars().all()
    print("purchase_lines count:", len(pl))
    for line in pl[:5]:
        print("  pl", line.id, "qty", line.quantity, "total", line.line_total, "unit", line.unit_price)

    avg = report_queries.avg_unit_cost_per_product(db)
    print("avg cost for product:", avg.get(pid))

    # reconcile
    changed = reconcile_all_stock_balances(db)
    db.commit()
    print("reconcile changed rows:", changed)
    balances2 = db.execute(
        select(StockBalance.warehouse_id, StockBalance.quantity).where(
            StockBalance.product_id == pid
        )
    ).all()
    print("stock_balances after reconcile:", balances2)
