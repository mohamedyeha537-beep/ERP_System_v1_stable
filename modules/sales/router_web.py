from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import POS_PRINT_CHOOSE_SIZE, SALES_CREATE
from modules.authz.service import user_has_permission
from modules.catalog.models import DiningTable, Product, ProductCategory, ProductKind
from modules.inventory.service import InsufficientStock
from modules.sales.models import Sale
from modules.payments.service import get_sale_payment
from modules.sales.receipt_layout import build_receipt_sections, sort_sale_lines_for_display
from modules.settings.service import (
    PAPER_SIZES,
    get_paper_css,
    get_setting,
    normalize_paper,
)
from modules.sales.service import (
    SalesError,
    add_or_increment_line_to_sale,
    cancel_sale,
    complete_sale,
    create_draft_sale,
    get_draft_sale,
    load_completed_sale_for_print,
    load_sale_with_lines,
    remove_line,
)

router = APIRouter(prefix="/pos", tags=["pos"])


def _cart_lines_ctx(db, sale: Sale | None, request: Request | None = None) -> dict:
    base = {
        "tables": _active_tables(db),
        "rooms": _active_rooms(db),
        "delivery_zones": [],
        "customer": None,
        "session_room_id": None,
    }
    if request is not None:
        try:
            base["session_room_id"] = request.session.get("draft_room_id")
        except Exception:  # noqa: BLE001
            base["session_room_id"] = None
    if sale is None:
        base["cart_lines"] = []
        return base
    from modules.delivery.service import list_zones

    base["delivery_zones"] = list_zones(db, only_active=True)
    base["cart_lines"] = sort_sale_lines_for_display(db, list(sale.lines))
    if sale.customer_id:
        from modules.customers.service import get_customer

        base["customer"] = get_customer(db, sale.customer_id)
    return base


def _active_tables(db) -> list[DiningTable]:
    return list(
        db.scalars(
            select(DiningTable)
            .where(DiningTable.is_active.is_(True))
            .order_by(DiningTable.sort_order, DiningTable.id)
        ).all()
    )


def _active_rooms(db) -> list:
    """قائمة الشقق/الغرف النشطة (للسياق ROOM في الـ POS)."""
    from modules.hotel.models import HotelRoom

    return list(
        db.scalars(
            select(HotelRoom)
            .where(HotelRoom.is_active.is_(True))
            .order_by(HotelRoom.number)
        ).all()
    )


def _ensure_draft(request: Request, db, user: User):
    raw = request.session.get("draft_sale_id")
    sale = None
    if raw is not None:
        sale = get_draft_sale(db, int(raw))
    if sale is None:
        sale = create_draft_sale(db, user.id)
        request.session["draft_sale_id"] = sale.id
        db.commit()
    return sale


def _parse_int(v: str | None, default: int | None = None) -> int | None:
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _category_tree_ids(db, root_id: int) -> set[int]:
    """معرّفات الفئة الجذر وجميع الفروع تحتها (لعدّ المنتجات)."""
    ids: set[int] = {root_id}
    stack = [root_id]
    while stack:
        pid = stack.pop()
        for cid in db.scalars(select(ProductCategory.id).where(ProductCategory.parent_id == pid)):
            if cid not in ids:
                ids.add(cid)
                stack.append(cid)
    return ids


def _root_product_counts(db, roots: list[ProductCategory]) -> dict[int, int]:
    out: dict[int, int] = {}
    for r in roots:
        tree = _category_tree_ids(db, r.id)
        n = db.scalar(
            select(func.count())
            .select_from(Product)
            .where(
                Product.kind == ProductKind.FINAL_SELLABLE,
                Product.is_active.is_(True),
                Product.category_id.in_(tree),
            )
        )
        out[r.id] = int(n or 0)
    return out


def _build_pos_panel(db, request: Request) -> dict:
    roots = list(
        db.scalars(
            select(ProductCategory)
            .where(ProductCategory.parent_id.is_(None))
            .options(selectinload(ProductCategory.children))
            .order_by(ProductCategory.sort_order, ProductCategory.id)
        ).all()
    )
    root_product_counts = _root_product_counts(db, roots)
    uncat = int(
        db.scalar(
            select(func.count())
            .select_from(Product)
            .where(
                Product.kind == ProductKind.FINAL_SELLABLE,
                Product.is_active.is_(True),
                Product.category_id.is_(None),
            )
        )
        or 0
    )

    root_q = _parse_int(request.query_params.get("root"))
    sub_q = _parse_int(request.query_params.get("sub"))

    root_ids = {r.id for r in roots}
    children: list[ProductCategory] = []
    root_sel: int | None = None
    sub_sel: int | None = None
    cat_filter: int | None = None

    if not roots and uncat == 0:
        products = list(
            db.scalars(
                select(Product)
                .where(
                    Product.kind == ProductKind.FINAL_SELLABLE,
                    Product.is_active.is_(True),
                )
                .order_by(Product.name_ar)
            ).all()
        )
        return {
            "roots": roots,
            "children": [],
            "root_sel": None,
            "sub_sel": None,
            "products": products,
            "uncat": uncat,
            "show_uncat_tile": False,
            "root_product_counts": root_product_counts,
        }

    if root_q == 0 and uncat > 0:
        root_sel = 0
        sub_sel = None
        cat_filter = None
        products = list(
            db.scalars(
                select(Product)
                .where(
                    Product.kind == ProductKind.FINAL_SELLABLE,
                    Product.is_active.is_(True),
                    Product.category_id.is_(None),
                )
                .order_by(Product.name_ar)
            ).all()
        )
        return {
            "roots": roots,
            "children": [],
            "root_sel": 0,
            "sub_sel": None,
            "products": products,
            "uncat": uncat,
            "show_uncat_tile": uncat > 0,
            "root_product_counts": root_product_counts,
        }

    if roots:
        root_sel = root_q if root_q in root_ids else roots[0].id
        children = list(
            db.scalars(
                select(ProductCategory)
                .where(ProductCategory.parent_id == root_sel)
                .order_by(ProductCategory.sort_order, ProductCategory.id)
            ).all()
        )
        child_ids = {c.id for c in children}
        if children:
            sub_sel = sub_q if sub_q in child_ids else children[0].id
            cat_filter = sub_sel
        else:
            sub_sel = None
            cat_filter = root_sel
        products = list(
            db.scalars(
                select(Product)
                .where(
                    Product.kind == ProductKind.FINAL_SELLABLE,
                    Product.is_active.is_(True),
                    Product.category_id == cat_filter,
                )
                .order_by(Product.name_ar)
            ).all()
        )
    else:
        root_sel = None
        products = list(
            db.scalars(
                select(Product)
                .where(
                    Product.kind == ProductKind.FINAL_SELLABLE,
                    Product.is_active.is_(True),
                    Product.category_id.is_(None),
                )
                .order_by(Product.name_ar)
            ).all()
        )

    return {
        "roots": roots,
        "children": children,
        "root_sel": root_sel,
        "sub_sel": sub_sel,
        "products": products,
        "uncat": uncat,
        "show_uncat_tile": uncat > 0,
        "root_product_counts": root_product_counts,
    }


@router.get("/panel", response_class=HTMLResponse)
def pos_panel_fragment(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    _ensure_draft(request, db, user)
    ctx = _build_pos_panel(db, request)
    ctx["request"] = request
    return templates.TemplateResponse("pos_panel.html", ctx)


@router.get("", response_class=HTMLResponse)
def pos_screen(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    sale = _ensure_draft(request, db, user)
    loaded = load_sale_with_lines(db, sale.id)
    if loaded:
        sale = loaded
    panel = _build_pos_panel(db, request)
    return templates.TemplateResponse(
        "pos.html",
        {"request": request, "sale": sale, "error": None, "feedback": None, **panel, **_cart_lines_ctx(db, sale, request)},
    )


@router.post("/add-line", response_class=HTMLResponse)
def pos_add_line(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    product_id: int = Form(...),
    quantity: str = Form(...),
):
    from modules.sales.models import SaleContext

    sale = _ensure_draft(request, db, user)

    # حماية: في سياق TABLE يجب اختيار طاولة قبل أي إضافة.
    if sale.context_type == SaleContext.TABLE and not sale.table_id:
        panel = _build_pos_panel(db, request)
        loaded = load_sale_with_lines(db, sale.id)
        if loaded:
            sale = loaded
        return templates.TemplateResponse(
            "pos.html",
            {
                "request": request,
                "sale": sale,
                "error": (
                    "اختر الطاولة أولاً من تبويب «طاولة» قبل إضافة أي صنف. "
                    "وإذا لم يكن الطلب على طاولة فحوّله إلى «خارجي»."
                ),
                "feedback": None,
                **panel,
                **_cart_lines_ctx(db, sale, request),
            },
            status_code=400,
        )

    # حماية: في سياق ROOM يجب اختيار شقة قبل أي إضافة.
    if (
        sale.context_type == SaleContext.ROOM
        and not request.session.get("draft_room_id")
    ):
        panel = _build_pos_panel(db, request)
        loaded = load_sale_with_lines(db, sale.id)
        if loaded:
            sale = loaded
        return templates.TemplateResponse(
            "pos.html",
            {
                "request": request,
                "sale": sale,
                "error": (
                    "اختر الشقة أولاً من تبويب «شقة» قبل إضافة أي صنف. "
                    "هذا يضمن أن الفاتورة تُقيَّد على الحساب الصحيح."
                ),
                "feedback": None,
                **panel,
                **_cart_lines_ctx(db, sale, request),
            },
            status_code=400,
        )

    try:
        qty = Decimal(quantity.strip())
        add_or_increment_line_to_sale(db, sale.id, product_id, qty, user.id)
        db.commit()
    except (SalesError, Exception) as e:
        db.rollback()
        panel = _build_pos_panel(db, request)
        sale = _ensure_draft(request, db, user)
        loaded = load_sale_with_lines(db, sale.id)
        if loaded:
            sale = loaded
        return templates.TemplateResponse(
            "pos.html",
            {"request": request, "sale": sale, "error": str(e), "feedback": None, **panel, **_cart_lines_ctx(db, sale, request)},
            status_code=400,
        )
    if request.headers.get("hx-request"):
        loaded = load_sale_with_lines(db, sale.id)
        if loaded:
            sale = loaded
        return templates.TemplateResponse(
            "pos_sidebar.html",
            {"request": request, "sale": sale, "feedback": None, **_cart_lines_ctx(db, sale, request)},
        )
    return RedirectResponse("/pos", status_code=302)


@router.post("/scan", response_class=HTMLResponse)
def pos_barcode_scan(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    barcode: str = Form(...),
):
    from modules.sales.models import SaleContext

    sale = _ensure_draft(request, db, user)
    code = barcode.strip()
    if not code:
        sx = load_sale_with_lines(db, sale.id) or sale
        return templates.TemplateResponse(
            "pos_sidebar.html",
            {"request": request, "sale": sx, "feedback": "أدخل باركوداً.", **_cart_lines_ctx(db, sx, request)},
        )

    # حماية: في سياق TABLE يجب اختيار طاولة قبل أي إضافة.
    if sale.context_type == SaleContext.TABLE and not sale.table_id:
        sx = load_sale_with_lines(db, sale.id) or sale
        return templates.TemplateResponse(
            "pos_sidebar.html",
            {
                "request": request,
                "sale": sx,
                "feedback": "⚠️ اختر الطاولة أولاً أو حوّل الطلب إلى «خارجي».",
                **_cart_lines_ctx(db, sx, request),
            },
        )

    # حماية: في سياق ROOM يجب اختيار شقة قبل أي إضافة.
    if (
        sale.context_type == SaleContext.ROOM
        and not request.session.get("draft_room_id")
    ):
        sx = load_sale_with_lines(db, sale.id) or sale
        return templates.TemplateResponse(
            "pos_sidebar.html",
            {
                "request": request,
                "sale": sx,
                "feedback": "⚠️ اختر الشقة أولاً قبل إضافة أي صنف.",
                **_cart_lines_ctx(db, sx, request),
            },
        )
    stmt = (
        select(Product)
        .where(
            Product.barcode == code,
            Product.kind == ProductKind.FINAL_SELLABLE,
            Product.is_active.is_(True),
        )
        .limit(1)
    )
    product = db.execute(stmt).scalar_one_or_none()
    if product is None:
        sx = load_sale_with_lines(db, sale.id) or sale
        return templates.TemplateResponse(
            "pos_sidebar.html",
            {"request": request, "sale": sx, "feedback": "لم يُعثر على منتج بهذا الباركود.", **_cart_lines_ctx(db, sx, request)},
        )
    try:
        add_or_increment_line_to_sale(db, sale.id, product.id, Decimal("1"), user.id)
        db.commit()
    except SalesError as e:
        db.rollback()
        sx = load_sale_with_lines(db, sale.id) or sale
        return templates.TemplateResponse(
            "pos_sidebar.html",
            {"request": request, "sale": sx, "feedback": str(e), **_cart_lines_ctx(db, sx, request)},
        )
    loaded = load_sale_with_lines(db, sale.id)
    assert loaded is not None
    return templates.TemplateResponse(
        "pos_sidebar.html",
        {"request": request, "sale": loaded, "feedback": None, **_cart_lines_ctx(db, loaded)},
    )


@router.post("/remove-line/{line_id}", response_class=HTMLResponse)
def pos_remove_line(
    request: Request,
    line_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    sale = _ensure_draft(request, db, user)
    try:
        remove_line(db, sale.id, line_id)
        db.commit()
    except SalesError:
        db.rollback()
    if request.headers.get("hx-request"):
        loaded = load_sale_with_lines(db, sale.id)
        if loaded:
            sale = loaded
        return templates.TemplateResponse(
            "pos_sidebar.html",
            {"request": request, "sale": sale, "feedback": None, **_cart_lines_ctx(db, sale, request)},
        )
    return RedirectResponse("/pos", status_code=302)


@router.post("/complete", response_class=HTMLResponse)
def pos_complete_redirect(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    """نقطة قديمة — تحوّل إلى صفحة اختيار الدفع لضمان تسجيل أسلوب دفع لكل بيع."""
    return RedirectResponse("/pos/checkout", status_code=302)


@router.get("/receipt/{sale_id}", response_class=HTMLResponse)
def print_receipt(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    autoprint: int = Query(0, ge=0, le=1),
    paper: str | None = Query(None),
):
    sale = load_completed_sale_for_print(db, sale_id)
    if sale is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="الفاتورة غير موجودة أو غير مكتملة.")
    sections = build_receipt_sections(db, sale)
    admin_paper = normalize_paper(get_setting(db, "print_paper_size", "A5"), "A5")
    can_choose = user_has_permission(user, POS_PRINT_CHOOSE_SIZE)
    chosen_paper = normalize_paper(paper, admin_paper) if (can_choose and paper) else admin_paper
    paper_css = get_paper_css(chosen_paper)
    store_name = get_setting(db, "store_name", "نقطة البيع")
    sale_payment = get_sale_payment(db, sale.id)
    payment_method_name = sale_payment.method.name_ar if (sale_payment and sale_payment.method) else None
    cashier = db.get(User, sale.created_by_id) if sale.created_by_id else None
    cashier_name = cashier.username if cashier else ""
    return templates.TemplateResponse(
        "receipt_print.html",
        {
            "request": request,
            "sale": sale,
            "sections": sections,
            "autoprint": autoprint,
            "paper": chosen_paper,
            "paper_css": paper_css,
            "paper_choices": PAPER_SIZES,
            "can_choose_paper": can_choose,
            "store_name": store_name,
            "payment_method_name": payment_method_name,
            "cashier_name": cashier_name,
        },
    )


@router.post("/set-table", response_class=HTMLResponse)
def pos_set_table(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    table_id: str = Form(""),
):
    sale = _ensure_draft(request, db, user)
    raw = (table_id or "").strip()
    if not raw:
        sale.table_id = None
    else:
        try:
            tid = int(raw)
        except ValueError:
            tid = None
        if tid is not None:
            t = db.get(DiningTable, tid)
            if t is not None and t.is_active:
                sale.table_id = tid
            else:
                sale.table_id = None
        else:
            sale.table_id = None
    db.commit()
    return RedirectResponse("/pos", status_code=302)


@router.post("/set-context", response_class=HTMLResponse)
def pos_set_context(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    context_type: str = Form("TABLE"),
    table_id: str = Form(""),
    room_id: str = Form(""),
    customer_phone: str = Form(""),
    customer_name: str = Form(""),
    external_order_type: str = Form("PICKUP"),
    delivery_zone_id: str = Form(""),
):
    """يضبط سياق الفاتورة: TABLE / ROOM / EXTERNAL.

    - TABLE: يربط بـ table_id
    - ROOM: يربط بـ room_id (سيُحوَّل لحساب غرفة عند checkout)
    - EXTERNAL: يربط بعميل عبر رقم الهاتف (يُنشأ إن لم يوجد)
    """
    from modules.delivery.service import get_zone
    from modules.sales.models import ExternalOrderType, SaleContext

    sale = _ensure_draft(request, db, user)
    ctx = (context_type or "TABLE").strip().upper()

    if ctx == "ROOM":
        from modules.authz.permissions import HOTEL_CHARGE
        from modules.hotel.models import HotelRoom

        if not user_has_permission(user, HOTEL_CHARGE):
            return RedirectResponse(
                "/pos?ctx_err=ليست لديك صلاحية القيد على غرفة.",
                status_code=302,
            )

        # نضبط السياق على ROOM دائماً (حتى لو لم تُحدَّد شقة بعد).
        # هذا يسمح بفتح التبويب أولاً ثم اختيار الشقة، ويمنع المتاجرة
        # عَرَضاً تحت سياق طاولة بينما الكاشير يظنّ أنه في تبويب الشقة.
        sale.context_type = SaleContext.ROOM
        sale.table_id = None
        sale.customer_id = None
        sale.external_order_type = ExternalOrderType.PICKUP
        sale.delivery_zone_id = None
        sale.delivery_zone_name = None
        sale.delivery_fee = Decimal("0")

        raw_rid = (room_id or "").strip()
        if not raw_rid:
            # تنظيف أي اختيار شقة سابق — الافتراضي: لا اختيار.
            request.session.pop("draft_room_id", None)
            request.session.pop("draft_room_guest", None)
            db.commit()
            return RedirectResponse("/pos", status_code=302)

        try:
            rid = int(raw_rid)
        except (TypeError, ValueError):
            rid = None
        room = db.get(HotelRoom, rid) if rid else None
        if room is None or not room.is_active:
            # نظّف أي اختيار قديم وارجع برسالة خطأ صريحة.
            request.session.pop("draft_room_id", None)
            request.session.pop("draft_room_guest", None)
            db.commit()
            return RedirectResponse(
                "/pos?ctx_err=الشقة غير صالحة. اختر شقة من القائمة.",
                status_code=302,
            )
        # اسم النزيل يُجمع لاحقاً عند زر «قيد على حساب الشقة وطباعة».
        request.session["draft_room_id"] = rid
        request.session["draft_room_guest"] = (customer_name or "").strip()
        db.commit()
        return RedirectResponse("/pos", status_code=302)

    if ctx == "EXTERNAL":
        from modules.customers.service import (
            CustomersError,
            get_or_create_by_phone,
            normalize_phone,
        )

        sale.context_type = SaleContext.EXTERNAL
        sale.table_id = None
        sale.external_order_type = ExternalOrderType.PICKUP
        sale.delivery_zone_id = None
        sale.delivery_zone_name = None
        sale.delivery_fee = Decimal("0")
        request.session.pop("draft_room_id", None)
        request.session.pop("draft_room_guest", None)
        ext_type_raw = (external_order_type or "PICKUP").strip().upper()
        ext_type = (
            ExternalOrderType.DELIVERY
            if ext_type_raw == ExternalOrderType.DELIVERY.value
            else ExternalOrderType.PICKUP
        )
        sale.external_order_type = ext_type
        if ext_type == ExternalOrderType.DELIVERY:
            zid = _parse_int((delivery_zone_id or "").strip())
            zone = get_zone(db, int(zid)) if zid else None
            if zone is None or not zone.is_active:
                db.rollback()
                return RedirectResponse(
                    "/pos?ctx_err=اختر منطقة توصيل صالحة قبل متابعة الطلب الخارجي.",
                    status_code=302,
                )
            sale.delivery_zone_id = zone.id
            sale.delivery_zone_name = zone.name_ar
            sale.delivery_fee = Decimal(str(zone.fee or 0)).quantize(Decimal("0.001"))
        phone_norm = normalize_phone(customer_phone)
        if phone_norm:
            try:
                c = get_or_create_by_phone(
                    db, phone=phone_norm, name=customer_name
                )
                sale.customer_id = c.id
            except CustomersError as e:
                db.rollback()
                return RedirectResponse(
                    f"/pos?ctx_err={e}", status_code=302
                )
        else:
            sale.customer_id = None
        db.commit()
        return RedirectResponse("/pos", status_code=302)

    # الافتراضي: TABLE
    sale.context_type = SaleContext.TABLE
    sale.customer_id = None
    sale.external_order_type = ExternalOrderType.PICKUP
    sale.delivery_zone_id = None
    sale.delivery_zone_name = None
    sale.delivery_fee = Decimal("0")
    request.session.pop("draft_room_id", None)
    request.session.pop("draft_room_guest", None)
    raw = (table_id or "").strip()
    if not raw:
        sale.table_id = None
    else:
        try:
            tid = int(raw)
        except ValueError:
            tid = None
        if tid is not None:
            t = db.get(DiningTable, tid)
            sale.table_id = tid if (t is not None and t.is_active) else None
        else:
            sale.table_id = None
    db.commit()
    return RedirectResponse("/pos", status_code=302)


@router.post("/charge-room", response_class=HTMLResponse)
def pos_charge_room(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    guest_name: str = Form(""),
    note: str = Form(""),
):
    """قيد طلب على حساب شقة فندق مباشرة من الـ sidebar — بدون شاشة دفع.

    يتطلّب صلاحية HOTEL_CHARGE وأن تكون مسودة الفاتورة في سياق ROOM
    وأن يكون room_id محفوظاً في الـ session (مختار من تبويب الشقة).

    الكاشير العادي يستخدم هذا الـ endpoint؛ موظف الاستقبال هو من يسوّي
    الحساب لاحقاً عبر `/hotel/settle` (يحتاج صلاحية HOTEL_SETTLE).
    """
    from modules.authz.permissions import HOTEL_CHARGE
    from modules.hotel.service import HotelError, open_room_charge
    from modules.hotel.models import HotelRoom
    from modules.sales.models import SaleContext

    if not user_has_permission(user, HOTEL_CHARGE):
        return RedirectResponse(
            "/pos?ctx_err=ليست لديك صلاحية القيد على حساب شقة.",
            status_code=302,
        )

    sale = _ensure_draft(request, db, user)
    loaded = load_sale_with_lines(db, sale.id)
    if loaded:
        sale = loaded

    if not sale.lines:
        return RedirectResponse(
            "/pos?ctx_err=أضِف صنفاً للسلة أولاً.", status_code=302
        )

    # رقم الشقة من الـ session (مُحدَّد سابقاً عبر تبويب الشقة)
    room_id_raw = request.session.get("draft_room_id")
    try:
        room_id = int(room_id_raw) if room_id_raw else None
    except (TypeError, ValueError):
        room_id = None
    if room_id is None:
        return RedirectResponse(
            "/pos?ctx_err=اختر الشقة أولاً من تبويب «شقة».",
            status_code=302,
        )
    room = db.get(HotelRoom, room_id)
    if room is None or not room.is_active:
        return RedirectResponse(
            "/pos?ctx_err=الشقة غير صالحة.", status_code=302
        )

    # اسم النزيل: من النموذج، أو من session كقيمة افتراضية
    guest = (guest_name or "").strip()
    if not guest:
        guest = (
            request.session.get("draft_room_guest", "") or ""
        ).strip()
    if not guest:
        return RedirectResponse(
            "/pos?ctx_err=اسم النزيل مطلوب.", status_code=302
        )

    try:
        complete_sale(db, sale.id, user.id)
        sale.context_type = SaleContext.ROOM
        open_room_charge(
            db,
            sale_id=sale.id,
            room_id=room_id,
            guest_name=guest,
            note=note,
            user_id=user.id,
        )
        db.commit()
    except (SalesError, HotelError) as e:
        db.rollback()
        return RedirectResponse(f"/pos?ctx_err={e}", status_code=302)
    except InsufficientStock as e:
        db.rollback()
        return RedirectResponse(f"/pos?ctx_err={e}", status_code=302)

    # توجيه طلبات الأقسام للمطبخ — مثل checkout العادي
    sale_id_for_bg = sale.id
    needs_wa_dispatch = False
    try:
        from modules.kds.service import create_tickets_for_sale

        tickets = create_tickets_for_sale(db, sale.id)
        db.commit()
        needs_wa_dispatch = any(
            t.delivery_status == "PENDING_SEND" for t in tickets
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        import logging

        logging.getLogger("kds").warning("ticket creation failed: %s", exc)

    if needs_wa_dispatch:
        from infra.background import run_in_background
        from modules.kds.service import dispatch_pending_whatsapp_tickets

        run_in_background(
            dispatch_pending_whatsapp_tickets,
            sale_id_for_bg,
            name="kds-whatsapp",
        )

    receipt_id = sale.id
    request.session.pop("draft_sale_id", None)
    request.session.pop("draft_room_id", None)
    request.session.pop("draft_room_guest", None)
    new_sale = create_draft_sale(db, user.id)
    request.session["draft_sale_id"] = new_sale.id
    db.commit()
    return RedirectResponse(
        f"/pos?ok=1&room=1&receipt={receipt_id}", status_code=302
    )


@router.post("/cancel-draft", response_class=HTMLResponse)
def pos_cancel_draft(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    raw = request.session.get("draft_sale_id")
    if raw:
        try:
            cancel_sale(db, int(raw))
            db.commit()
        except SalesError:
            db.rollback()
    request.session.pop("draft_sale_id", None)
    new_sale = create_draft_sale(db, user.id)
    request.session["draft_sale_id"] = new_sale.id
    db.commit()
    return RedirectResponse("/pos", status_code=302)
