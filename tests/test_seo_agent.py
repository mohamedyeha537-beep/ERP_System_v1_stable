"""اختبارات SEO Agent Integration."""
from __future__ import annotations

import json
import os
import re

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./tests/test_seo.db"
os.environ["SECRET_KEY"] = "test-secret-key-32-characters-long"
os.environ["DEFAULT_ADMIN_USERNAME"] = "admin"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "admin123"
os.environ["SEO_AGENT_ENABLED"] = "true"
os.environ["SEO_ENVIRONMENT"] = "staging"
os.environ["SEO_AGENT_API_KEY"] = "test-seo-api-key-not-real"
os.environ["SEO_ALLOW_AUTO_APPLY"] = "false"
os.environ["SEO_REQUIRE_PRODUCTION_APPROVAL"] = "true"
os.environ["SEO_PUBLIC_BASE_URL"] = "https://pos.baytak.ly"


def _apply_seo_env() -> None:
    os.environ["DATABASE_URL"] = "sqlite:///./tests/test_seo.db"
    os.environ["SEO_AGENT_ENABLED"] = "true"
    os.environ["SEO_ENVIRONMENT"] = "staging"
    os.environ["SEO_AGENT_API_KEY"] = "test-seo-api-key-not-real"
    os.environ["SEO_PUBLIC_BASE_URL"] = "https://pos.baytak.ly"


from modules.seo.config import clear_seo_config_cache  # noqa: E402

clear_seo_config_cache()

from app.main import create_app  # noqa: E402
from infra.config import get_settings  # noqa: E402
from infra.db import reset_engine  # noqa: E402


HEADERS_OK = {
    "X-SEO-API-Key": "test-seo-api-key-not-real",
    "X-SEO-Environment": "staging",
    "X-Request-Id": "test-req-1",
}


@pytest.fixture
def client():
    _apply_seo_env()
    clear_seo_config_cache()
    get_settings.cache_clear()
    reset_engine()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_engine()
    get_settings.cache_clear()
    clear_seo_config_cache()


def _csrf(client: TestClient) -> str:
    page = client.get("/auth/login")
    m = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    if not m:
        m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert m, "csrf missing"
    return m.group(1)


def _login(client: TestClient) -> str:
    token = _csrf(client)
    client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123", "csrf_token": token},
        follow_redirects=False,
    )
    # بعد الدخول يُجدَّد رمز CSRF في الجلسة — خذه من صفحة محمية
    page = client.get("/admin/seo")
    m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    if not m:
        m = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert m, "csrf missing after login"
    return m.group(1)


def _csrf_headers(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


def test_health_ok_and_rejects_bad_key(client: TestClient) -> None:
    bad = client.get("/api/seo/health", headers={**HEADERS_OK, "X-SEO-API-Key": "wrong"})
    assert bad.status_code == 401
    ok = client.get("/api/seo/health", headers=HEADERS_OK)
    assert ok.status_code == 200
    data = ok.json()
    assert data["ok"] is True
    assert data["environment"] == "staging"


def test_environment_mismatch_rejected(client: TestClient) -> None:
    r = client.get(
        "/api/seo/health",
        headers={**HEADERS_OK, "X-SEO-Environment": "production"},
    )
    assert r.status_code == 403


def test_public_pages_and_sitemap(client: TestClient) -> None:
    r = client.get("/api/seo/public-pages", headers=HEADERS_OK)
    assert r.status_code == 200
    body = r.json()
    assert "pages" in body
    assert body["environment"] == "staging"
    urls = [p["url"] for p in body["pages"]]
    assert any("/shop" in u for u in urls)

    sm = client.get("/sitemap.xml")
    assert sm.status_code == 200
    assert "urlset" in sm.text
    assert client.get("/robots.txt").status_code == 200
    assert client.get("/sitemap-products.xml").status_code == 200
    assert client.get("/sitemap-rooms.xml").status_code == 200
    assert client.get("/sitemap-images.xml").status_code == 200


def test_staging_apply_after_approval_and_rollback(client: TestClient) -> None:
    from infra.db import get_engine
    from sqlalchemy.orm import sessionmaker
    from modules.catalog.models import Product, ProductKind

    engine = get_engine()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        product = Product(
            name_ar="اختبار SEO",
            unit="قطعة",
            kind=ProductKind.FINAL_SELLABLE,
            is_active=True,
            show_in_shop=True,
            seo_title="عنوان قديم",
        )
        db.add(product)
        db.commit()
        db.refresh(product)
        pid = product.id
    finally:
        db.close()

    sug = client.post(
        "/api/seo/suggestions",
        headers=HEADERS_OK,
        json={
            "suggestions": [
                {
                    "page_url": f"https://pos.baytak.ly/shop?product={pid}",
                    "page_type": "product",
                    "entity_type": "product",
                    "entity_id": pid,
                    "suggestion_type": "meta_title",
                    "current_value": "عنوان قديم",
                    "suggested_value": "عنوان جديد SEO",
                    "create_fix": True,
                }
            ]
        },
    )
    assert sug.status_code == 200, sug.text
    fix_id = sug.json()["items"][0]["fix_id"]
    assert fix_id

    csrf = _login(client)
    appr = client.post(
        f"/api/seo/fixes/{fix_id}/approve",
        headers=_csrf_headers(csrf),
        follow_redirects=False,
    )
    assert appr.status_code == 200, appr.text

    apply = client.post(
        f"/api/seo/fixes/{fix_id}/apply",
        headers=_csrf_headers(csrf),
        follow_redirects=False,
    )
    assert apply.status_code == 200, apply.text
    assert apply.json()["status"] == "applied"

    db = Session()
    try:
        product = db.get(Product, pid)
        assert product is not None
        assert product.seo_title == "عنوان جديد SEO"
    finally:
        db.close()

    rb = client.post(
        f"/api/seo/fixes/{fix_id}/rollback",
        headers=_csrf_headers(csrf),
        follow_redirects=False,
    )
    assert rb.status_code == 200, rb.text
    assert rb.json()["status"] == "rolled_back"

    db = Session()
    try:
        product = db.get(Product, pid)
        assert product is not None
        assert product.seo_title == "عنوان قديم"
    finally:
        db.close()


def test_production_rejects_apply_without_prod_flag(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SEO_ENVIRONMENT", "production")
    monkeypatch.setenv("SEO_PUBLIC_BASE_URL", "https://baytak.baytak.ly")
    monkeypatch.setenv("SEO_ALLOW_AUTO_APPLY", "false")
    monkeypatch.setenv("SEO_REQUIRE_PRODUCTION_APPROVAL", "true")
    clear_seo_config_cache()

    from infra.db import get_engine
    from sqlalchemy.orm import sessionmaker
    from modules.catalog.models import Product, ProductKind
    from modules.seo.models import SeoFixRequest, SeoPage
    import json as _json

    engine = get_engine()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        product = Product(
            name_ar="Prod SEO",
            unit="قطعة",
            kind=ProductKind.FINAL_SELLABLE,
            is_active=True,
            show_in_shop=True,
            seo_title="old",
        )
        db.add(product)
        db.flush()
        page = SeoPage(
            environment="production",
            page_type="product",
            entity_type="product",
            entity_id=product.id,
            url=f"https://baytak.baytak.ly/shop?product={product.id}",
            title="Prod SEO",
        )
        db.add(page)
        db.flush()
        fix = SeoFixRequest(
            environment="production",
            page_id=page.id,
            fix_type="meta_title",
            payload_json=_json.dumps(
                {
                    "value": "new title",
                    "entity_type": "product",
                    "entity_id": product.id,
                }
            ),
            status="approved",
            approved_for_production=False,
        )
        db.add(fix)
        db.commit()
        fix_id = fix.id
    finally:
        db.close()

    csrf = _login(client)
    apply = client.post(
        f"/api/seo/fixes/{fix_id}/apply",
        headers=_csrf_headers(csrf),
        follow_redirects=False,
    )
    assert apply.status_code == 403

    # restore staging for other tests in same process
    monkeypatch.setenv("SEO_ENVIRONMENT", "staging")
    monkeypatch.setenv("SEO_PUBLIC_BASE_URL", "https://pos.baytak.ly")
    clear_seo_config_cache()


def test_admin_seo_and_pos_not_broken(client: TestClient) -> None:
    _login(client)
    assert client.get("/admin/seo").status_code == 200
    assert client.get("/admin/seo/pages").status_code == 200
    assert client.get("/auth/login").status_code == 200
    # POS/hotel entry points still respond (redirect or 200)
    r = client.get("/")
    assert r.status_code in (200, 302)
