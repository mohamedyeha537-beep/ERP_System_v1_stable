from decimal import Decimal

from pydantic import BaseModel, Field


class OnlineOrderLine(BaseModel):
    product_id: int = Field(..., description="معرّف المنتج النهائي في نقطة البيع")
    quantity: Decimal = Field(..., gt=0)


class OnlineOrderIn(BaseModel):
    external_order_id: str = Field(..., min_length=1, max_length=120)
    lines: list[OnlineOrderLine] = Field(..., min_length=1)


class OnlineOrderOut(BaseModel):
    sale_id: int
    status: str
    total: Decimal
    message: str = "تم استلام الطلب"
