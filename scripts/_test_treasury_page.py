"""Quick test: treasury page data + template render."""
from decimal import Decimal

from starlette.requests import Request

from app.jinja_env import templates
from app.main import create_app
from infra.db import get_session_factory
from modules.payments.models import PaymentMethodKind
from modules.payments.treasury_service import (
    daily_balance_rows,
    kind_current_balance,
    list_ledger_entries,
    treasury_summaries,
)
from modules.settings.service import get_setting


def main() -> None:
    create_app()
    db = get_session_factory()()
    try:
        kind = PaymentMethodKind.CASH
        entries = list_ledger_entries(db, kind)
        daily = daily_balance_rows(db, kind)
        summ = treasury_summaries(db).get("CASH")
        scope = {
            "request": Request({"type": "http", "method": "GET", "path": "/"}),
            "kind": "cash",
            "kind_label": "test",
            "current_balance": kind_current_balance(db, kind),
            "summary": summ,
            "entries": entries,
            "daily_rows": daily,
            "filter_dir": "all",
            "filter_day": "",
            "store_name": get_setting(db, "store_name", "x"),
        }
        html = templates.get_template("pos_treasury.html").render(scope)
        print("OK", len(html), "entries", len(entries))
    finally:
        db.close()


if __name__ == "__main__":
    main()
