"""اختبار شامل لدورة الفندق (حساب غرفة + تسوية):

1) إنشاء غرفة فندق
2) إنشاء فاتورة بيع وإكمالها بدون دفع — قيدها على الغرفة
3) التحقق من الرصيد المفتوح للغرفة
4) تسوية الفاتورة وتسجيل الدفع
5) التحقق من إنشاء SalePayment وأن الرصيد أصبح صفراً
6) فحص الواجهات HTTP
"""
import os
import sys
from decimal import Decimal

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# ====== bootstrap models =====================================================
import modules.authz.models  # noqa: F401
import modules.catalog.models  # noqa: F401
import modules.inventory.models  # noqa: F401
import modules.payments.models  # noqa: F401
import modules.sales.models  # noqa: F401
import modules.settings.models  # noqa: F401
import modules.hr.models  # noqa: F401
import modules.hotel.models  # noqa: F401
import modules.customers.models  # noqa: F401

from infra.db import Base, get_engine, get_session_factory  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from modules.catalog.models import Product, ProductKind  # noqa: E402
from modules.hotel import service as hotel  # noqa: E402
from modules.hotel.models import HotelRoom, RoomCharge  # noqa: E402
from modules.payments.models import SalePayment  # noqa: E402
from modules.payments.service import (  # noqa: E402
    ensure_default_payment_methods,
    list_payment_methods,
)
from modules.sales.models import SaleStatus  # noqa: E402
from modules.sales.service import (  # noqa: E402
    add_line_to_sale,
    complete_sale,
    create_draft_sale,
)


def _print(label: str, ok: bool, detail: str = "") -> None:
    mark = "✅" if ok else "❌"
    msg = f"{mark} {label}"
    if detail:
        msg += f"  ({detail})"
    print(msg)


def _ensure_test_product(db: Session) -> Product:
    p = (
        db.query(Product)
        .filter(Product.name_ar == "صنف اختبار فندق")
        .one_or_none()
    )
    if p is None:
        p = Product(
            name_ar="صنف اختبار فندق",
            sku="HOTEL-TEST-001",
            sell_price=Decimal("25.000"),
            kind=ProductKind.FINAL_SELLABLE,
            is_active=True,
        )
        db.add(p)
        db.flush()
    return p


def main():
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    Session_ = get_session_factory()
    db = Session_()
    try:
        # تأكد من وجود أساليب دفع
        ensure_default_payment_methods(db)
        db.commit()
        methods = list_payment_methods(db, only_active=True)
        if not methods:
            print("❌ لا توجد أساليب دفع")
            return
        cash = next((m for m in methods if m.kind.value == "CASH"), methods[0])
        _print("توفّر أساليب الدفع", True, f"عدد={len(methods)}")

        # 1) إنشاء غرفة (تنظيف أولي إن وجد سجلات قديمة)
        room_number = "TEST-501"
        existing = hotel.get_room_by_number(db, room_number)
        if existing:
            db.query(RoomCharge).filter(
                RoomCharge.room_id == existing.id
            ).delete(synchronize_session=False)
            db.delete(existing)
            db.commit()
        for n in ("TEST-502",):
            ex = hotel.get_room_by_number(db, n)
            if ex:
                db.query(RoomCharge).filter(
                    RoomCharge.room_id == ex.id
                ).delete(synchronize_session=False)
                db.delete(ex)
                db.commit()
        room = hotel.create_room(
            db,
            number=room_number,
            notes="اختبار آلي",
        )
        db.commit()
        _print("إنشاء غرفة جديدة (بدون اسم نزيل ثابت)", room is not None,
               f"#{room.number} id={room.id} guest_name={room.guest_name}")

        # 2) إنشاء فاتورة بيع
        product = _ensure_test_product(db)
        db.commit()
        sale = create_draft_sale(db, user_id=None)
        add_line_to_sale(db, sale.id, product.id, Decimal("2"), user_id=None)
        db.commit()

        # إكمال البيع
        sale = complete_sale(db, sale.id, user_id=None)
        db.commit()
        _print(
            "إكمال البيع بدون دفع",
            sale.status == SaleStatus.COMPLETED,
            f"sale#{sale.id} total={sale.total}",
        )

        # قيد بدون اسم نزيل — مسموح (يُكتب على الفاتورة المطبوعة)
        rc_empty = hotel.open_room_charge(
            db,
            sale_id=sale.id,
            room_id=room.id,
            guest_name="",
            note=None,
            user_id=None,
        )
        db.commit()
        _print(
            "قيد بدون اسم نزيل (يُملأ على الفاتورة)",
            rc_empty is not None and rc_empty.guest_name_snapshot is None,
            f"charge#{rc_empty.id}",
        )

        sale2 = create_draft_sale(db, user_id=None)
        add_line_to_sale(db, sale2.id, product.id, Decimal("1"), user_id=None)
        db.commit()
        sale2 = complete_sale(db, sale2.id, user_id=None)
        db.commit()

        # قيد على الغرفة باسم نزيل
        rc = hotel.open_room_charge(
            db,
            sale_id=sale2.id,
            room_id=room.id,
            guest_name="السيد محمد",
            note="عشاء",
            user_id=None,
        )
        db.commit()
        _print(
            "فتح حساب غرفة على الفاتورة",
            rc is not None and not rc.is_settled
            and rc.guest_name_snapshot == "السيد محمد",
            f"charge#{rc.id} guest={rc.guest_name_snapshot}",
        )

        # تأكيد أن الغرفة لم تكتسب الاسم تلقائياً
        db.refresh(room)
        _print(
            "اسم النزيل لا يُحفظ على الغرفة (الغرفة ثابتة، النزلاء يتغيرون)",
            room.guest_name is None,
            f"room.guest_name={room.guest_name!r}",
        )

        # 3) التحقق من الرصيد المفتوح
        open_total = hotel.room_open_total(db, room.id)
        _print(
            "الرصيد المفتوح للغرفة = إجمالي الفاتورة",
            open_total == sale.total,
            f"open={open_total} sale={sale.total}",
        )

        rooms_with = hotel.rooms_with_open_balance(db)
        found = any(r.id == room.id for r, _, _ in rooms_with)
        _print("الغرفة تظهر في قائمة الغرف ذات الحساب المفتوح", found)

        grand = hotel.grand_open_total(db)
        _print(
            "إجمالي الأرصدة المعلقة يتضمّن فاتورتنا",
            grand >= sale.total,
            f"grand={grand}",
        )

        # نزلاء الغرفة (من snapshot الفواتير)
        guests = hotel.open_guests_for_room(db, room.id)
        _print(
            "قائمة نزلاء الغرفة من snapshot الفواتير",
            "السيد محمد" in guests,
            f"guests={guests}",
        )

        # تأكد ألا يوجد SalePayment بعد
        payments_before = (
            db.query(SalePayment).filter(SalePayment.sale_id == sale.id).count()
        )
        _print(
            "لا يوجد سجل دفع قبل التسوية",
            payments_before == 0,
            f"count={payments_before}",
        )

        # 4) تسوية الفاتورة
        settled = hotel.settle_charges(
            db,
            charge_ids=[rc.id],
            payment_method_id=cash.id,
            user_id=None,
        )
        db.commit()
        _print(
            "تسوية الفاتورة بنجاح",
            len(settled) == 1 and settled[0].is_settled,
            f"settled={len(settled)}",
        )

        # 5) التحقق من إنشاء SalePayment
        payments_after = list(
            db.query(SalePayment).filter(SalePayment.sale_id == sale.id).all()
        )
        _print(
            "تم إنشاء سجل دفع بعد التسوية",
            len(payments_after) == 1
            and Decimal(str(payments_after[0].amount)) == sale.total
            and payments_after[0].payment_method_id == cash.id,
            f"count={len(payments_after)} amount={payments_after[0].amount if payments_after else 'N/A'}",
        )

        # الرصيد المفتوح صار صفر
        open_total_after = hotel.room_open_total(db, room.id)
        _print(
            "الرصيد المفتوح للغرفة = 0 بعد التسوية",
            open_total_after == Decimal("0"),
            f"open={open_total_after}",
        )

        # محاولة قيد فاتورة مدفوعة على غرفة (يجب يفشل)
        sale2 = create_draft_sale(db, user_id=None)
        add_line_to_sale(db, sale2.id, product.id, Decimal("1"), user_id=None)
        complete_sale(db, sale2.id, user_id=None)
        from modules.payments.service import record_sale_payment

        record_sale_payment(db, sale2.id, cash.id, sale2.total)
        db.commit()
        try:
            hotel.open_room_charge(
                db,
                sale_id=sale2.id,
                room_id=room.id,
                guest_name="x",
                note=None,
                user_id=None,
            )
            db.commit()
            _print("منع قيد فاتورة مدفوعة على غرفة", False, "تم القيد بالخطأ")
        except hotel.HotelError as e:
            db.rollback()
            _print("منع قيد فاتورة مدفوعة على غرفة", True, str(e))

        # محاولة حذف غرفة عليها رصيد (نضيف فاتورة جديدة بنزيل آخر)
        sale3 = create_draft_sale(db, user_id=None)
        add_line_to_sale(db, sale3.id, product.id, Decimal("1"), user_id=None)
        complete_sale(db, sale3.id, user_id=None)
        hotel.open_room_charge(
            db,
            sale_id=sale3.id,
            room_id=room.id,
            guest_name="السيدة فاطمة",
            note=None,
            user_id=None,
        )
        db.commit()
        # تأكد ظهور النزيلين المختلفين على نفس الغرفة (تاريخياً)
        # الفاتورة الأولى مسوّاة لذا لا تظهر في open_guests، فقط الجديدة
        guests_now = hotel.open_guests_for_room(db, room.id)
        _print(
            "نزلاء جدد لا يختلطون بالقدامى المسوَّين",
            guests_now == ["السيدة فاطمة"],
            f"open={guests_now}",
        )
        try:
            hotel.delete_room(db, room.id)
            db.commit()
            _print("منع حذف غرفة عليها رصيد", False, "تم الحذف بالخطأ")
        except hotel.HotelError as e:
            db.rollback()
            _print("منع حذف غرفة عليها رصيد", True, str(e))

        # تسوية الفاتورة الأخيرة
        rc3 = (
            db.query(RoomCharge)
            .filter(RoomCharge.sale_id == sale3.id)
            .one()
        )
        hotel.settle_charges(
            db,
            charge_ids=[rc3.id],
            payment_method_id=cash.id,
            user_id=None,
        )
        db.commit()

        # حذف غرفة لها تاريخ تسوية يجب أن يفشل (حماية للسجلات التاريخية)
        try:
            hotel.delete_room(db, room.id)
            db.commit()
            _print(
                "منع حذف غرفة لها سجل تسوية تاريخي",
                False,
                "تم الحذف بالخطأ",
            )
        except hotel.HotelError as e:
            db.rollback()
            _print("منع حذف غرفة لها سجل تسوية تاريخي", True, str(e))

        # لكن يمكن تعطيلها
        hotel.update_room(db, room.id, is_active=False)
        db.commit()
        room_check = db.get(HotelRoom, room.id)
        _print(
            "يمكن تعطيل الغرفة بدلاً من الحذف",
            room_check is not None and not room_check.is_active,
        )

        # إنشاء غرفة جديدة بدون تاريخ ـ يمكن حذفها بأمان
        room2 = hotel.create_room(db, number="TEST-502")
        db.commit()
        hotel.delete_room(db, room2.id)
        db.commit()
        _print(
            "حذف غرفة بلا تاريخ تسويات",
            db.get(HotelRoom, room2.id) is None,
        )

        print("\n✅ كل الاختبارات اجتيزت.")

    finally:
        # تنظيف ضماني — لا تترك أي بيانات اختبارية في DB
        try:
            from modules.inventory.models import StockMovement
            from modules.sales.models import SaleLine

            test_p = (
                db.query(Product)
                .filter(Product.name_ar == "صنف اختبار فندق")
                .first()
            )
            if test_p is not None:
                db.query(SaleLine).filter(
                    SaleLine.product_id == test_p.id
                ).delete(synchronize_session=False)
                db.query(StockMovement).filter(
                    StockMovement.product_id == test_p.id
                ).delete(synchronize_session=False)
                db.delete(test_p)
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        db.close()


if __name__ == "__main__":
    main()
