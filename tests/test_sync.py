"""اختبارات مزامنة أوفلاين/أونلاين."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./tests/test_sync.db"
os.environ["SECRET_KEY"] = "test-secret-key-32-characters-long"
os.environ["DEFAULT_ADMIN_USERNAME"] = "admin"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "admin123"
os.environ["SYNC_ENABLED"] = "true"
os.environ["SYNC_SITE_ID"] = "site-test"
os.environ["ONLINE_SYNC_API_KEY"] = "sync-key-123"

from app.main import create_app
from infra.config import get_settings
from infra.db import reset_engine


def _fresh_db() -> None:
    reset_engine()
    get_settings.cache_clear()
    for extra in ("", "-shm", "-wal"):
        path = "./tests/test_sync.db" + extra
        if os.path.exists(path):
            os.remove(path)


@pytest.fixture
def client():
    _fresh_db()
    app = create_app()
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> None:
    response = client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=False,
    )
    assert response.status_code == 302


def test_sync_status_shows_pending_event(client: TestClient) -> None:
    """إنشاء صنف محلي يُسجّل حدث مزامنة واحد."""
    _login(client)
    response = client.post(
        "/catalog/products/new",
        data={
            "name_ar": "صنف مزامنة",
            "sell_price": "10.000",
            "kind": "FINAL_SELLABLE",
            "unit": "قطعة",
            "is_active": "on",
            "show_in_pos": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    status = client.get("/admin/sync/status")
    assert status.status_code == 200
    data = status.json()
    assert data["enabled"] is True
    assert data["site_id"] == "site-test"
    assert data["pending_count"] == 1


def test_sync_push_applies_remote_product_and_sale(client: TestClient) -> None:
    """السيرفر المركزي يستقبل منتج وفاتورة من موقع بعيد ويطبقها."""
    product_payload = (
        '{"id": 10, "name_ar": "صنف بعيد", "sku": "", "barcode": null, "unit": "قطعة", "kind": "FINAL_SELLABLE", '
        '"sell_price": "20.000", "reorder_level": "0", "is_active": true, "show_in_pos": true, '
        '"direct_purchase_enabled": true, "notes": null, "category_id": null, "sales_warehouse_id": null, '
        '"kitchen_department_id": null, "kitchen_section_id": null, "image_filename": null, '
        '"line_modifier_presets": null, "expiry_tracked": false, "expiry_production_date": null, '
        '"expiry_date": null, "expiry_warn_days": 7, "price_linked_to_bom": false, "bom_markup_pct": null, '
        '"reference_unit_cost": null}'
    )
    sale_payload = (
        '{"id": 99, "ref": "ORD-99", "status": "COMPLETED", "source": "ONLINE", "business_domain": "general", '
        '"total": "40.000", "lines": [{"product_id": 10, "quantity": 2}]}'
    )

    r1 = client.post(
        "/api/sync/push",
        json={
            "events": [
                {
                    "event_id": "p10",
                    "site_id": "remote-site",
                    "action": "INSERT",
                    "table_name": "products",
                    "record_id": "10",
                    "payload_json": product_payload,
                    "source_updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
        },
        headers={"X-Sync-API-Key": "sync-key-123"},
    )
    assert r1.status_code == 200
    assert r1.json()["results"][0]["applied"] is True

    r2 = client.post(
        "/api/sync/push",
        json={
            "events": [
                {
                    "event_id": "s99",
                    "site_id": "remote-site",
                    "action": "INSERT",
                    "table_name": "sales",
                    "record_id": "99",
                    "payload_json": sale_payload,
                    "source_updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
        },
        headers={"X-Sync-API-Key": "sync-key-123"},
    )
    assert r2.status_code == 200
    assert r2.json()["results"][0]["applied"] is True


def test_sync_pull_returns_remote_events(client: TestClient) -> None:
    """`/api/sync/pull` يرجع أحداث المواقع الأخرى للموقع الطالب."""
    product_payload = (
        '{"id": 11, "name_ar": "صنف آخر", "sku": "", "barcode": null, "unit": "قطعة", "kind": "FINAL_SELLABLE", '
        '"sell_price": "5.000", "reorder_level": "0", "is_active": true, "show_in_pos": true, '
        '"direct_purchase_enabled": true, "notes": null, "category_id": null, "sales_warehouse_id": null, '
        '"kitchen_department_id": null, "kitchen_section_id": null, "image_filename": null, '
        '"line_modifier_presets": null, "expiry_tracked": false, "expiry_production_date": null, '
        '"expiry_date": null, "expiry_warn_days": 7, "price_linked_to_bom": false, "bom_markup_pct": null, '
        '"reference_unit_cost": null}'
    )
    client.post(
        "/api/sync/push",
        json={
            "events": [
                {
                    "event_id": "p11",
                    "site_id": "another-site",
                    "action": "INSERT",
                    "table_name": "products",
                    "record_id": "11",
                    "payload_json": product_payload,
                    "source_updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
        },
        headers={"X-Sync-API-Key": "sync-key-123"},
    )

    response = client.post(
        "/api/sync/pull",
        json={"last_event_id": None, "limit": 10},
        headers={"X-Sync-API-Key": "sync-key-123"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["has_more"] is False
    events = data["events"]
    assert len(events) >= 1
    assert any(e["site_id"] != "site-test" for e in events)


def test_sync_push_rejects_invalid_key(client: TestClient) -> None:
    response = client.post(
        "/api/sync/push",
        json={"events": []},
        headers={"X-Sync-API-Key": "wrong-key"},
    )
    assert response.status_code == 401
