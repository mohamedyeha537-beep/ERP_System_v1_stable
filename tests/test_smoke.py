"""اختبارات دخان أساسية للتأكد من إقلاع التطبيق وعمل نقاط الدخول الأساسية."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./tests/test.db"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["DEFAULT_ADMIN_USERNAME"] = "admin"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "admin123"

from app.main import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_health_or_redirect(client: TestClient) -> None:
    """الصفحة الرئيسية تعيد توجيه إلى صفحة الدخول أو تعرض لوحة التحكم."""
    response = client.get("/")
    assert response.status_code in (200, 302)


def test_login_page_loads(client: TestClient) -> None:
    response = client.get("/auth/login")
    assert response.status_code == 200


def test_login_with_default_admin(client: TestClient) -> None:
    response = client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers.get("location") == "/"


def test_dashboard_after_login(client: TestClient) -> None:
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    response = client.get("/")
    assert response.status_code == 200


def test_gl_pages_after_login(client: TestClient) -> None:
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    for path in (
        "/admin/gl/accounts",
        "/admin/gl/journal",
        "/admin/gl/reports",
    ):
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"


def test_gl_fiscal_and_opening_balances(client: TestClient) -> None:
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    assert client.get("/admin/gl/fiscal").status_code == 200
    assert client.post(
        "/admin/gl/fiscal",
        data={"name": "2026", "start_date": "2026-01-01", "end_date": "2026-12-31"},
        follow_redirects=False,
    ).status_code == 303
    assert client.get("/admin/gl/opening-balances").status_code == 200
