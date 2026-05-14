from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.dashboard_stats import collect as collect_dashboard_stats
from app.deps import get_current_user
from app.jinja_env import templates
from infra.config import get_settings
from infra.db import Base, get_engine
from infra.sqlite_patch import patch_sqlite_schema
from modules.authz.router_web import admin_router, router as auth_router
from modules.authz.service import (
    ensure_demo_users,
    seed_if_empty,
    sync_permissions,
    user_has_permission,
)
from modules.backup.router_web import router as backup_router
from modules.catalog.categories_router import router as categories_router
from modules.catalog.router_web import router as catalog_router
from modules.catalog.service import ensure_default_units
from modules.integration.router_api import router as integration_router
from modules.inventory.router_web import router as inventory_router
from modules.alerts.router_web import router as alerts_router
from modules.payments.router_web import (
    assets_router as payments_assets_router,
    checkout_router as payments_checkout_router,
    expenses_router as payments_expenses_router,
    purchases_router as payments_purchases_router,
    recurring_router as payments_recurring_router,
    router as payments_admin_router,
)
from modules.payments.service import ensure_default_payment_methods
from modules.reporting.router_web import router as reporting_router
from modules.kds.router_web import router as kds_router
from modules.hr.router_web import (
    advances_router as hr_advances_router,
    attendance_router as hr_attendance_router,
    employees_router as hr_employees_router,
    payroll_router as hr_payroll_router,
)
from modules.hotel.router_web import (
    rooms_router as hotel_rooms_router,
    settle_router as hotel_settle_router,
)
from modules.customers.router_web import (
    router as customers_admin_router,
    loyalty_router as loyalty_settings_router,
)
from modules.delivery.router_web import router as delivery_router
from modules.branding.router_web import router as branding_router
from modules.refunds.router_web import router as refunds_router
from modules.sales.router_web import router as pos_router
from modules.settings.router_web import router as settings_router
from modules.tables.router_web import router as tables_router
from modules.settings.service import ensure_default_settings
from sqlalchemy.orm import sessionmaker


@asynccontextmanager
async def lifespan(app: FastAPI):
    import modules.authz.models  # noqa: F401
    import modules.catalog.models  # noqa: F401
    import modules.inventory.models  # noqa: F401
    import modules.payments.models  # noqa: F401
    import modules.sales.models  # noqa: F401
    import modules.settings.models  # noqa: F401
    import modules.hr.models  # noqa: F401
    import modules.hotel.models  # noqa: F401
    import modules.customers.models  # noqa: F401
    import modules.delivery.models  # noqa: F401
    import modules.refunds.models  # noqa: F401

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    patch_sqlite_schema(engine)
    upload_root = Path(__file__).resolve().parent / "static" / "uploads" / "products"
    upload_root.mkdir(parents=True, exist_ok=True)
    branding_root = Path(__file__).resolve().parent / "static" / "uploads" / "branding"
    branding_root.mkdir(parents=True, exist_ok=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = Session()
    try:
        seed_if_empty(db)
        sync_permissions(db)
        ensure_demo_users(db)
        ensure_default_units(db)
        ensure_default_settings(db)
        ensure_default_payment_methods(db)
    finally:
        db.close()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="نقطة البيع", lifespan=lifespan)

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
        request.state.current_user = None
        request.state.store_name = "نقطة البيع"
        request.state.brand = None
        try:
            uid = (
                request.session.get("user_id")
                if hasattr(request, "session") and "user_id" in request.session
                else None
            )
            from infra.db import get_session_factory
            from modules.authz.models import User as _User
            from modules.branding.service import get_branding
            from modules.settings.service import get_setting

            Session = get_session_factory()
            db = Session()
            try:
                # الهويّة البصرية: تُحمَّل دائماً (حتى قبل تسجيل الدخول لصفحة الـ login)
                try:
                    request.state.brand = get_branding(db)
                except Exception:
                    request.state.brand = None
                # اسم المتجر: يُحمَّل دائماً
                try:
                    request.state.store_name = get_setting(
                        db, "store_name", "نقطة البيع"
                    )
                except Exception:
                    pass
                # المستخدم الحالي: فقط إن كان مسجَّلاً
                if uid:
                    u = db.get(_User, int(uid))
                    if u is not None and u.is_active:
                        db.refresh(u, ["roles"])
                        for r in u.roles:
                            db.refresh(r, ["permissions"])
                        request.state.current_user = u
            finally:
                db.close()
        except Exception:
            pass
        return await call_next(request)

    # SessionMiddleware يجب أن يُضاف بعد المخصَّص ليُنفَّذ أولاً
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=settings.session_cookie_name,
    )

    static_dir = Path(__file__).resolve().parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(catalog_router)
    app.include_router(categories_router, prefix="/catalog")
    app.include_router(inventory_router)
    app.include_router(pos_router)
    app.include_router(reporting_router)
    app.include_router(delivery_router)
    app.include_router(integration_router)
    app.include_router(settings_router)
    app.include_router(payments_admin_router)
    app.include_router(payments_purchases_router)
    app.include_router(payments_expenses_router)
    app.include_router(payments_assets_router)
    app.include_router(payments_recurring_router)
    app.include_router(payments_checkout_router)
    app.include_router(alerts_router)
    app.include_router(tables_router)
    app.include_router(kds_router)
    app.include_router(backup_router)
    app.include_router(hr_employees_router)
    app.include_router(hr_attendance_router)
    app.include_router(hr_payroll_router)
    app.include_router(hr_advances_router)
    app.include_router(hotel_rooms_router)
    app.include_router(hotel_settle_router)
    app.include_router(customers_admin_router)
    app.include_router(loyalty_settings_router)
    app.include_router(branding_router)
    app.include_router(refunds_router)

    from app.deps import DBSession

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        db: DBSession,
        user=Depends(get_current_user),
    ):
        if user is None:
            return RedirectResponse("/auth/login", status_code=302)

        def perm(code: str) -> bool:
            return user_has_permission(user, code)

        try:
            stats = collect_dashboard_stats(db)
        except Exception:
            stats = None

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "perm": perm,
                "stats": stats,
            },
        )

    return app


app = create_app()
