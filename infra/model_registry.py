"""استيراد كل نماذج SQLAlchemy — لتسجيل الجداول في Base.metadata."""

from __future__ import annotations


def import_all_models() -> None:
    import modules.authz.models  # noqa: F401
    import modules.catalog.models  # noqa: F401
    import modules.customers.models  # noqa: F401
    import modules.dashboard_notify.models  # noqa: F401
    import modules.delivery.models  # noqa: F401
    import modules.gl.models  # noqa: F401
    import modules.hotel.booking_models  # noqa: F401
    import modules.hotel.models  # noqa: F401
    import modules.hotel.store_models  # noqa: F401
    import modules.hr.models  # noqa: F401
    import modules.inventory.models  # noqa: F401
    import modules.kds.models  # noqa: F401
    import modules.messaging.models  # noqa: F401
    import modules.notifications.models  # noqa: F401
    import modules.payments.models  # noqa: F401
    import modules.pos_shifts.models  # noqa: F401
    import modules.printing.models  # noqa: F401
    import modules.refunds.models  # noqa: F401
    import modules.sales.models  # noqa: F401
    import modules.settings.models  # noqa: F401
    import modules.shop.models  # noqa: F401
    import modules.web_marketing.models  # noqa: F401
