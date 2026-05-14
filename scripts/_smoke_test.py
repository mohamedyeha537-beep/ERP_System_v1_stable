"""فحص دخان (Smoke test) لجميع الصفحات الرئيسية."""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient
from app.main import app

c = TestClient(app)
r = c.post(
    "/auth/login",
    data={"username": "admin", "password": "admin123"},
    follow_redirects=False,
)
print(f"  login: {r.status_code}")

URLS = [
    "/",
    "/pos",
    "/admin",
    "/admin/customers",
    "/admin/loyalty",
    "/admin/hotel/rooms",
    "/hotel/settle",
    "/admin/tables",
    "/admin/payment-methods",
    "/admin/purchases",
    "/admin/expenses",
    "/admin/assets",
    "/admin/recurring-costs",
    "/admin/employees",
    "/admin/attendance",
    "/admin/payroll",
    "/admin/advances",
    "/admin/settings",
    "/admin/backup",
    "/inventory",
    "/reports/sales",
    "/reports/purchases",
    "/reports/expenses",
    "/reports/profit",
    "/reports/inventory",
    "/reports/comprehensive",
    "/reports/break-even",
    "/admin/alerts",
    "/admin/branding",
    "/admin/kds",
]

ok = 0
errors = []
for url in URLS:
    r = c.get(url)
    status = "✓" if r.status_code == 200 else "✗"
    print(f"  {status} {r.status_code}  {url}")
    if r.status_code == 200:
        ok += 1
    else:
        errors.append((url, r.status_code))

print(f"\n  {ok}/{len(URLS)} pages OK")
if errors:
    print("  failed:", errors)
sys.exit(0 if not errors else 1)
