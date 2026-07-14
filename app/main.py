from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.dashboard_stats import collect as collect_dashboard_stats
from app.security_headers import SecurityHeadersMiddleware
from app.deps import get_current_user
from app.jinja_env import templates
from infra.config import get_settings
from infra.db import get_engine
from infra.schema_bootstrap import bootstrap_schema
from modules.authz.router_web import admin_router, router as auth_router
from modules.authz.service import (
    ensure_demo_users,
    ensure_purchases_clerk_demo_user,
    seed_if_empty,
    sync_permissions,
    user_has_permission,
)
from modules.backup.router_web import router as backup_router
from modules.catalog.categories_router import router as categories_router
from modules.catalog.import_export_router import router as catalog_import_router
from modules.catalog.router_web import router as catalog_router
from modules.catalog.service import ensure_default_units
from modules.integration.router_api import router as integration_router
from modules.inventory.router_web import router as inventory_router
from modules.inventory.warehouses_router import router as warehouses_router
from modules.admin.schema_fix_router import router as schema_fix_router
from modules.alerts.router_web import router as alerts_router
from modules.payments.asset_edit_router import router as asset_edit_router
from modules.payments.purchase_edit_router import router as purchase_edit_router
from modules.payments.consumable_categories_router import (
    router as consumable_categories_router,
)
from modules.payments.router_web import (
    assets_router as payments_assets_router,
    checkout_router as payments_checkout_router,
    expenses_router as payments_expenses_router,
    purchases_router as payments_purchases_router,
    recurring_router as payments_recurring_router,
    consumables_router as payments_consumables_router,
    router as payments_admin_router,
)
from modules.payments.service import ensure_default_payment_methods, ensure_hotel_treasury_payment_methods
from modules.reporting.router_web import router as reporting_router
from modules.receivables.router_web import router as receivables_router
from modules.payables.router_web import router as payables_router
from modules.kds.departments_router import router as kitchen_departments_router
from modules.kds.router_web import router as kds_router
from modules.printing.router_api import router as print_agent_api_router
from modules.printing.router_web import router as printing_admin_router
from modules.hr.router_web import (
    advances_router as hr_advances_router,
    attendance_router as hr_attendance_router,
    departments_router as hr_departments_router,
    employees_router as hr_employees_router,
    payroll_router as hr_payroll_router,
    work_shifts_router as hr_work_shifts_router,
)
from modules.hr.meal_router import router as hr_meal_router
from modules.hotel.router_web import (
    rooms_router as hotel_rooms_router,
    settle_router as hotel_settle_router,
)
from modules.hotel.router_bookings import bookings_router as hotel_bookings_router
from modules.hotel.router_shifts import shifts_router as hotel_shifts_router
from modules.hotel.router_portal import stay_api as hotel_stay_api_router
from modules.hotel.router_portal import stay_router as hotel_stay_router
from modules.hotel.router_store import suites_api as hotel_suites_api_router
from modules.hotel.router_store import suites_router as hotel_suites_router
from modules.customers.router_web import (
    router as customers_admin_router,
    loyalty_router as loyalty_settings_router,
)
from modules.delivery.router_web import router as delivery_router
from modules.branding.router_web import router as branding_router
from modules.web_marketing.router_web import router as web_marketing_router
from modules.web_marketing.router_api import router as web_analytics_api_router
from modules.refunds.router_web import router as refunds_router
from modules.sales.invoice_edit_router import router as sales_invoice_edit_router
from modules.pos_shifts.router_web import router as pos_shifts_router
from modules.pos_shifts.reports_router import router as pos_shift_reports_router
from modules.admin_shifts_router import router as admin_shifts_router
from modules.pos_shifts.expense_categories_router import router as shift_expense_categories_router
from modules.sales.router_web import router as pos_router
from modules.gl.router_web import router as gl_router
from modules.settings.router_web import router as settings_router
from modules.messaging.router_web import router as messaging_router
from modules.notifications.admin_routes import router as notifications_router
from modules.dashboard_notify.router_web import router as activity_hub_router
from modules.messaging.router_api import router as messaging_api_router
from modules.messaging.router_inbox import router as messaging_inbox_router
from modules.messaging.router_web_chat import admin_router as web_chat_admin_router
from modules.messaging.router_web_chat import api_router as web_chat_api_router
from modules.messaging.router_web_chat import router as web_chat_router
from modules.shop.router_web import api_router as shop_api_router
from modules.shop.router_web import router as shop_router
from modules.tables.router_web import router as tables_router
from modules.settings.service import ensure_default_settings
from sqlalchemy.orm import sessionmaker


@asynccontextmanager
async def lifespan(app: FastAPI):
    import sys

    print("DB bootstrap starting... (wait for Application startup complete)", flush=True)
    import modules.authz.models  # noqa: F401
    import modules.catalog.models  # noqa: F401
    import modules.inventory.models  # noqa: F401
    import modules.payments.models  # noqa: F401
    import modules.sales.models  # noqa: F401
    import modules.settings.models  # noqa: F401
    import modules.hr.models  # noqa: F401
    import modules.hotel.models  # noqa: F401
    import modules.hotel.booking_models  # noqa: F401
    import modules.hotel.shift_models  # noqa: F401
    import modules.customers.models  # noqa: F401
    import modules.delivery.models  # noqa: F401
    import modules.refunds.models  # noqa: F401
    import modules.pos_shifts.models  # noqa: F401
    import modules.printing.models  # noqa: F401
    import modules.dashboard_notify.models  # noqa: F401
    import modules.kds.models  # noqa: F401
    import modules.messaging.models  # noqa: F401
    import modules.notifications.models  # noqa: F401
    import modules.gl.models  # noqa: F401
    import modules.shop.models  # noqa: F401
    import modules.web_marketing.models  # noqa: F401

    engine = get_engine()
    bootstrap_schema(engine)
    try:
        from infra.catalog_schema import repair_catalog_schema

        added = repair_catalog_schema(engine)
        if added:
            print(f"Catalog schema repaired at startup: {added}", flush=True)
    except Exception as exc:
        print(f"WARN catalog schema repair at startup: {exc}", flush=True)
    upload_root = Path(__file__).resolve().parent / "static" / "uploads" / "products"
    upload_root.mkdir(parents=True, exist_ok=True)
    purchase_inv_root = Path(__file__).resolve().parent / "static" / "uploads" / "purchases"
    purchase_inv_root.mkdir(parents=True, exist_ok=True)
    (purchase_inv_root / "supplier_invoices").mkdir(parents=True, exist_ok=True)
    (purchase_inv_root / "payment_receipts").mkdir(parents=True, exist_ok=True)
    branding_root = Path(__file__).resolve().parent / "static" / "uploads" / "branding"
    branding_root.mkdir(parents=True, exist_ok=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = Session()
    try:
        seed_if_empty(db)
        sync_permissions(db)
        ensure_demo_users(db)
        ensure_purchases_clerk_demo_user(db)
        ensure_default_units(db)
        ensure_default_settings(db)
        from modules.sales.order_policy import ensure_order_policy_defaults

        ensure_order_policy_defaults(db)
        ensure_default_payment_methods(db)
        ensure_hotel_treasury_payment_methods(db)
        from modules.hr.meal_allowance import ensure_staff_meal_payment_method

        ensure_staff_meal_payment_method(db)
        from modules.platform.module_registry import ensure_default_modules

        ensure_default_modules(db)
        from modules.hotel.booking_service import (
            ensure_default_cancellation_policy,
            ensure_default_property,
            ensure_default_room_types,
        )

        ensure_default_property(db)
        ensure_default_room_types(db)
        ensure_default_cancellation_policy(db)
        from modules.hotel.rate_plans import ensure_default_rate_plans

        ensure_default_rate_plans(db)
        db.commit()
        from modules.payments.service import (
            ensure_owner_equity_payment_method,
            ensure_supplier_credit_payment_method,
        )

        ensure_supplier_credit_payment_method(db)
        ensure_owner_equity_payment_method(db)
        from modules.payments.shift_handoff_service import ensure_main_treasury_payment_methods

        ensure_main_treasury_payment_methods(db)
        from modules.inventory.service import ensure_main_warehouse

        ensure_main_warehouse(db)
        from modules.kds.sections_seed import (
            ensure_default_kitchen_sections,
            sync_catalog_kitchen_routing,
        )

        ensure_default_kitchen_sections(db)
        sync_catalog_kitchen_routing(db)
        from modules.messaging.seed import ensure_messaging_defaults

        ensure_messaging_defaults(db)
        from modules.notifications.seed import ensure_notification_defaults

        ensure_notification_defaults(db)
        from modules.gl.seed import ensure_default_chart_of_accounts, ensure_default_fiscal_year

        ensure_default_chart_of_accounts(db)
        ensure_default_fiscal_year(db)
        from modules.hr.zkbio_config import zkbio_install_detected
        from modules.settings.service import get_setting, set_setting

        if zkbio_install_detected() and get_setting(db, "zk_sync_enabled") == "0":
            if not get_setting(db, "zk_last_sync_at"):
                set_setting(db, "zk_sync_enabled", "1")
        db.commit()
    finally:
        db.close()

    import threading

    from infra.background import with_db

    def _messaging_worker() -> None:
        import time as _time

        while True:
            interval = 30
            try:
                with with_db() as db:
                    from modules.messaging.outbox import (
                        pending_outbox_count,
                        process_outbox_batch,
                    )
                    from modules.settings.service import get_bool, get_int

                    interval = max(
                        10, get_int(db, "messaging_worker_interval_seconds", 30)
                    )
                    if get_bool(db, "messaging_enabled", False) and get_bool(
                        db, "messaging_outbox_worker_enabled", True
                    ):
                        sent = process_outbox_batch(db)
                        db.commit()
                        if sent > 0:
                            interval = min(interval, 10)
                        elif pending_outbox_count(db) > 0:
                            interval = min(interval, 10)
                    if get_bool(db, "notifications_enabled", True):
                        from modules.notifications.worker import run_cycle

                        run_cycle(db)
                        db.commit()
            except Exception:
                pass
            _time.sleep(interval)

    def _zk_sync_worker() -> None:
        import time as _time

        while True:
            interval = 30
            try:
                with with_db() as db:
                    from modules.hr.zkbio_config import zkbio_install_detected
                    from modules.hr.zkbio_sync import sync_zkbio_punches
                    from modules.settings.service import get_bool, get_int

                    interval = max(15, get_int(db, "zk_sync_interval_seconds", 30))
                    if get_bool(db, "zk_sync_enabled", zkbio_install_detected()):
                        sync_zkbio_punches(db)
                        db.commit()
            except Exception:
                pass
            _time.sleep(interval)

    threading.Thread(
        target=_messaging_worker, daemon=True, name="messaging-outbox"
    ).start()
    threading.Thread(target=_zk_sync_worker, daemon=True, name="zkbio-sync").start()

    yield


def create_app() -> FastAPI:
    settings = get_settings()
    docs_url = "/docs" if settings.show_docs else None
    redoc_url = "/redoc" if settings.show_docs else None
    openapi_url = "/openapi.json" if settings.show_docs else None
    app = FastAPI(
        title="نقطة البيع",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )

    _SKIP_STATE_PREFIXES = ("/static/", "/uploads/")
    _LIGHT_STATE_PREFIXES = ("/pos/live", "/pos/web-chat-rails", "/shop", "/api/shop", "/suites", "/api/suites", "/stay/my", "/api/web-analytics")

    # =====================================================================
    # ترتيب الـ middlewares في FastAPI: الأخير المُضاف يُنفَّذ أولاً عند
    # دخول الطلب. لذا نضيف middleware تحميل المستخدم *قبل* SessionMiddleware
    # حتى يُنفَّذ SessionMiddleware أولاً ويُتيح request.session لدينا.
    # =====================================================================
    @app.middleware("http")
    async def _attach_request_state(request: Request, call_next):
        """يحمّل المستخدم الحالي + اسم المتجر إلى request.state.

        يُستخدم في القوالب عبر دوال `current_user`/`has_perm`/`store_name`
        المعرَّفة كـ globals في `app/jinja_env.py`.
        """
        path = request.url.path
        if path.startswith(_SKIP_STATE_PREFIXES):
            return await call_next(request)

        request.state.current_user = None
        request.state.store_name = "نقطة البيع"
        request.state.brand = None
        request.state._user_attached = True
        light = any(path.startswith(p) for p in _LIGHT_STATE_PREFIXES)
        try:
            uid = (
                request.session.get("user_id")
                if hasattr(request, "session") and "user_id" in request.session
                else None
            )
            if light and not uid:
                return await call_next(request)

            from infra.db import get_session_factory
            from modules.authz.models import User as _User
            from modules.settings.service import get_setting

            Session = get_session_factory()
            db = Session()
            try:
                u = None
                if uid:
                    u = db.get(_User, int(uid))
                    if u is not None and u.is_active:
                        request.state.current_user = u
                    else:
                        u = None
                if not light:
                    try:
                        from modules.branding.service import resolve_active_branding

                        session = (
                            request.session
                            if hasattr(request, "session")
                            else None
                        )
                        brand = resolve_active_branding(
                            db, user=u, session=session
                        )
                        request.state.brand = brand
                        request.state.store_name = brand.get("pos_label") or get_setting(
                            db, "store_name", "نقطة البيع"
                        )
                    except Exception:
                        request.state.brand = None
                        try:
                            request.state.store_name = get_setting(
                                db, "store_name", "نقطة البيع"
                            )
                        except Exception:
                            pass
            finally:
                db.close()
        except Exception:
            pass
        return await call_next(request)

    @app.middleware("http")
    async def _domain_scope_guard(request: Request, call_next):
        """يحصر موظف الفندق/المطعم في مسارات مجاله."""
        from fastapi.responses import RedirectResponse
        from modules.authz.domain_scope import domain_scope_redirect_path

        user = getattr(request.state, "current_user", None)
        if user:
            target = domain_scope_redirect_path(user, request.url.path)
            if target and request.url.path != target:
                return RedirectResponse(target, status_code=302)
        return await call_next(request)

    @app.middleware("http")
    async def _cashier_kiosk_guard(request: Request, call_next):
        """يحصر حساب الكاشير في شاشة نقطة البيع فقط."""
        from fastapi.responses import RedirectResponse
        from modules.authz.kiosk import is_cashier_kiosk_user, kiosk_allowed_path

        user = getattr(request.state, "current_user", None)
        if user and is_cashier_kiosk_user(user):
            if not kiosk_allowed_path(request.url.path):
                return RedirectResponse("/pos", status_code=302)
        return await call_next(request)

    @app.middleware("http")
    async def _dashboard_info_section_seen(request: Request, call_next):
        """أقسام إعلامية: تُمسح الشارة عند فتح الصفحة (لكل مستخدم)."""
        response = await call_next(request)
        if request.method != "GET" or response.status_code >= 400:
            return response
        user = getattr(request.state, "current_user", None)
        if user is None:
            return response
        from modules.dashboard_notify.constants import INFO_ONLY_SECTIONS
        from modules.dashboard_notify.service import mark_seen_for_path, section_for_path

        key = section_for_path(request.url.path)
        if not key or key not in INFO_ONLY_SECTIONS:
            return response
        try:
            from infra.db import get_session_factory

            Session = get_session_factory()
            db = Session()
            try:
                mark_seen_for_path(db, user.id, request.url.path)
                db.commit()
            finally:
                db.close()
        except Exception:
            pass
        return response

    # SessionMiddleware يجب أن يُضاف بعد المخصَّص ليُنفَّذ أولاً
    app.add_middleware(GZipMiddleware, minimum_size=500)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=settings.session_cookie_name,
        max_age=settings.session_max_age,
        same_site=settings.session_same_site,  # type: ignore[arg-type]
        https_only=settings.session_https_only,
    )

    static_dir = Path(__file__).resolve().parent / "static"
    static_dir.mkdir(exist_ok=True)
    uploads_dir = static_dir / "uploads"
    uploads_dir.mkdir(exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(admin_shifts_router)
    app.include_router(catalog_router)
    app.include_router(categories_router, prefix="/catalog")
    app.include_router(catalog_import_router, prefix="/catalog")
    app.include_router(inventory_router)
    app.include_router(warehouses_router)
    app.include_router(pos_router)
    app.include_router(pos_shifts_router)
    app.include_router(pos_shift_reports_router)
    app.include_router(shift_expense_categories_router)
    app.include_router(reporting_router)
    app.include_router(receivables_router)
    app.include_router(payables_router)
    app.include_router(delivery_router)
    app.include_router(integration_router)
    app.include_router(settings_router)
    app.include_router(schema_fix_router)
    app.include_router(gl_router)
    app.include_router(payments_admin_router)
    app.include_router(payments_purchases_router)
    app.include_router(payments_expenses_router)
    app.include_router(payments_assets_router)
    app.include_router(payments_consumables_router)
    app.include_router(consumable_categories_router)
    app.include_router(asset_edit_router)
    app.include_router(purchase_edit_router)
    app.include_router(payments_recurring_router)
    app.include_router(payments_checkout_router)
    app.include_router(alerts_router)
    app.include_router(messaging_router)
    app.include_router(notifications_router)
    app.include_router(activity_hub_router)
    app.include_router(messaging_inbox_router)
    app.include_router(messaging_api_router)
    app.include_router(web_chat_router)
    app.include_router(web_chat_api_router)
    app.include_router(web_chat_admin_router)
    app.include_router(shop_router)
    app.include_router(shop_api_router)
    app.include_router(tables_router)
    app.include_router(kds_router)
    app.include_router(kitchen_departments_router)
    app.include_router(printing_admin_router)
    app.include_router(print_agent_api_router)
    app.include_router(backup_router)
    app.include_router(hr_employees_router)
    app.include_router(hr_departments_router)
    app.include_router(hr_work_shifts_router)
    app.include_router(hr_attendance_router)
    app.include_router(hr_payroll_router)
    app.include_router(hr_advances_router)
    app.include_router(hr_meal_router)
    app.include_router(hotel_rooms_router)
    app.include_router(hotel_bookings_router)
    app.include_router(hotel_shifts_router)
    app.include_router(hotel_stay_router)
    app.include_router(hotel_stay_api_router)
    app.include_router(hotel_suites_router)
    app.include_router(hotel_suites_api_router)
    app.include_router(hotel_settle_router)
    app.include_router(customers_admin_router)
    app.include_router(loyalty_settings_router)
    app.include_router(branding_router)
    app.include_router(web_marketing_router)
    app.include_router(web_analytics_api_router)
    app.include_router(refunds_router)
    app.include_router(sales_invoice_edit_router)

    from sqlalchemy.exc import OperationalError, ProgrammingError

    def _is_missing_column_error(exc: BaseException) -> bool:
        msg = str(exc).lower()
        return "no such column" in msg or "unknown column" in msg

    @app.exception_handler(OperationalError)
    @app.exception_handler(ProgrammingError)
    async def _repair_schema_on_column_error(request: Request, exc: Exception):
        """إصلاح تلقائي ثم إعادة تحميل الصفحة عند نقص عمود في MySQL/SQLite."""
        if not _is_missing_column_error(exc):
            raise exc
        if request.query_params.get("_schema_repaired"):
            raise exc
        from infra.catalog_schema import repair_catalog_schema
        from infra.schema_bootstrap import reset_schema_patch_flag, ensure_schema_patched

        reset_schema_patch_flag()
        try:
            ensure_schema_patched(force=True)
            repair_catalog_schema(get_engine())
        except Exception:
            pass
        # POST/PUT لا يُعاد كـ GET على نفس المسار (مثل /delete) وإلا يظهر Method Not Allowed.
        method = (request.method or "GET").upper()
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            referer = (request.headers.get("referer") or "").strip()
            if referer.startswith("/") or referer.startswith("http://") or referer.startswith("https://"):
                sep = "&" if "?" in referer else "?"
                return RedirectResponse(
                    f"{referer}{sep}_schema_repaired=1",
                    status_code=303,
                )
            path = request.url.path.rstrip("/")
            if path.endswith("/delete"):
                path = path[: -len("/delete")]
                # /admin/employees/10 → /admin/employees
                if path.rsplit("/", 1)[-1].isdigit():
                    path = path.rsplit("/", 1)[0]
            return RedirectResponse(
                f"{path or '/'}?_schema_repaired=1",
                status_code=303,
            )
        url = str(request.url.include_query_params(_schema_repaired="1"))
        return RedirectResponse(url, status_code=303)

    from app.deps import DBSession

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        db: DBSession,
        user=Depends(get_current_user),
    ):
        if user is None:
            return RedirectResponse("/auth/login", status_code=302)

        from modules.authz.kiosk import is_cashier_kiosk_user

        if is_cashier_kiosk_user(user):
            return RedirectResponse("/pos", status_code=302)

        def perm(code: str) -> bool:
            return user_has_permission(user, code)

        from modules.platform.business_domain import domain_label, resolve_finance_domain

        finance_domain = resolve_finance_domain(user, request.session)

        try:
            stats = collect_dashboard_stats(db, domain=finance_domain)
        except Exception as exc:
            import logging

            logging.getLogger("pos.dashboard").exception(
                "فشل تحميل إحصائيات اللوحة: %s", exc
            )
            stats = None

        from modules.dashboard_notify.service import badge_counts

        try:
            badges = badge_counts(db, user.id)
        except Exception:
            badges = {}

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "perm": perm,
                "stats": stats,
                "badge_counts": badges,
                "finance_domain_label": domain_label(finance_domain)
                if finance_domain
                else "الكل",
            },
        )

    return app


app = create_app()
