"""تنظيف بيانات الاختبار التي أُنشئت آلياً.

يحذف:
- أصناف التي تحتوي على كلمة "اختبار" في الاسم
- غرف فندق برقم يبدأ بـ "TEST" أو "HTTP-TEST"
- الفواتير المرتبطة بأصناف الاختبار (إن وُجدت)
- الفئات الفارغة بعد الحذف

⚠️ يُشغّل يدوياً فقط عند الحاجة، لا يُستدعى آلياً.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import modules.authz.models  # noqa: F401, E402
import modules.catalog.models  # noqa: F401, E402
import modules.customers.models  # noqa: F401, E402
import modules.hotel.models  # noqa: F401, E402
import modules.hr.models  # noqa: F401, E402
import modules.inventory.models  # noqa: F401, E402
import modules.payments.models  # noqa: F401, E402
import modules.sales.models  # noqa: F401, E402
import modules.settings.models  # noqa: F401, E402

from infra.db import get_session_factory  # noqa: E402
from modules.catalog.models import Product, ProductCategory  # noqa: E402
from modules.hotel.models import HotelRoom, RoomCharge  # noqa: E402
from modules.inventory.models import StockMovement  # noqa: E402
from modules.payments.models import SalePayment  # noqa: E402
from modules.sales.models import Sale, SaleLine  # noqa: E402


def main():
    Session = get_session_factory()
    db = Session()
    try:
        # 1) غرف الاختبار
        rooms = (
            db.query(HotelRoom)
            .filter(
                (HotelRoom.number.like("TEST-%"))
                | (HotelRoom.number.like("HTTP-TEST-%"))
            )
            .all()
        )
        for r in rooms:
            charges = db.query(RoomCharge).filter(RoomCharge.room_id == r.id).all()
            for c in charges:
                db.delete(c)
            db.delete(r)
            print(f"  - حذف غرفة #{r.number}")
        if rooms:
            db.commit()

        # 2) أصناف الاختبار
        products = (
            db.query(Product)
            .filter(Product.name_ar.like("%اختبار%"))
            .all()
        )
        for p in products:
            # احذف خطوط البيع المرتبطة
            lines = db.query(SaleLine).filter(SaleLine.product_id == p.id).all()
            sale_ids = {ln.sale_id for ln in lines}
            for ln in lines:
                db.delete(ln)
            # احذف حركات المخزون
            movements = (
                db.query(StockMovement)
                .filter(StockMovement.product_id == p.id)
                .all()
            )
            for m in movements:
                db.delete(m)
            # احذف الأصناف نفسها
            db.delete(p)
            print(f"  - حذف صنف: {p.name_ar} (id={p.id})")

            # احذف الفواتير الفارغة الناتجة
            for sid in sale_ids:
                rem = (
                    db.query(SaleLine).filter(SaleLine.sale_id == sid).count()
                )
                if rem == 0:
                    sale = db.get(Sale, sid)
                    if sale:
                        db.query(SalePayment).filter(
                            SalePayment.sale_id == sid
                        ).delete(synchronize_session=False)
                        db.query(RoomCharge).filter(
                            RoomCharge.sale_id == sid
                        ).delete(synchronize_session=False)
                        db.delete(sale)
                        print(f"    · حذف فاتورة فارغة #{sid}")
        if products:
            db.commit()

        # 3) فئات الاختبار صراحةً (تحتوي على «اختبار»)
        test_cats = (
            db.query(ProductCategory)
            .filter(ProductCategory.name_ar.like("%اختبار%"))
            .all()
        )
        for c in test_cats:
            # احذف ما تبقّى من أصناف داخلها (احتياطي)
            for p in (
                db.query(Product).filter(Product.category_id == c.id).all()
            ):
                db.query(SaleLine).filter(
                    SaleLine.product_id == p.id
                ).delete(synchronize_session=False)
                db.query(StockMovement).filter(
                    StockMovement.product_id == p.id
                ).delete(synchronize_session=False)
                db.delete(p)
                print(f"  - حذف صنف داخل فئة اختبار: {p.name_ar}")
            db.delete(c)
            print(f"  - حذف فئة اختبار: {c.name_ar}")
        if test_cats:
            db.commit()

        # 4) الفئات الفارغة (بدون أبناء أو أصناف) — احتياطي
        cats = db.query(ProductCategory).all()
        removed_cats = 0
        for c in cats:
            child_count = (
                db.query(ProductCategory)
                .filter(ProductCategory.parent_id == c.id)
                .count()
            )
            product_count = (
                db.query(Product)
                .filter(Product.category_id == c.id)
                .count()
            )
            if child_count == 0 and product_count == 0:
                db.delete(c)
                removed_cats += 1
                print(f"  - حذف فئة فارغة: {c.name_ar}")
        if removed_cats:
            db.commit()

        print("\n✅ التنظيف اكتمل.")
        if not (rooms or products or removed_cats):
            print("   لا توجد بيانات اختبار.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
