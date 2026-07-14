"""احتساب مواد التغليف — طلبات أونلاين / استلام / توصيل فقط."""
from __future__ import annotations

from modules.sales.models import ExternalOrderType, Sale, SaleContext, SaleSource


def sale_requires_packaging(sale: Sale) -> bool:
    """هل يستهلك هذا الطلب مواد تغليف؟"""
    if sale.source == SaleSource.ONLINE:
        return True
    if sale.context_type == SaleContext.EXTERNAL:
        return sale.external_order_type in (
            ExternalOrderType.PICKUP,
            ExternalOrderType.DELIVERY,
        )
    return False
