from starlette.requests import Request
from app.jinja_env import templates

scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
request = Request(scope)
try:
    html = templates.get_template("reports_financial_operational.html").render(
        {
            "request": request,
            "period": "month",
            "period_labels": {"month": "هذا الشهر"},
            "start": None,
            "end": None,
            "start_str": "",
            "end_str": "",
            "stmt": type(
                "S",
                (),
                {
                    "invoice_count": 0,
                    "return_count": 0,
                    "gross_revenue": 0,
                    "returns_total": 0,
                    "net_revenue": 0,
                    "collections_total": 0,
                    "refunds_payment_total": 0,
                    "net_cash_effect": 0,
                    "taxes_included": 0,
                    "line_discounts_included": 0,
                },
            )(),
            "logical": [],
            "filters": type(
                "F", (), {"user_id": None, "payment_method_id": None, "customer_id": None}
            )(),
            "users": [],
            "payment_methods": [],
            "customers": [],
            "filter_qs": "period=month",
            "filter_extra": "",
            "nav_active": "financial_ops",
        }
    )
    print("OK", len(html))
except Exception:
    import traceback

    traceback.print_exc()
