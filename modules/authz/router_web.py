from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.deps import DBSession, require_permission, LoggedInUser
from app.jinja_env import templates
from modules.authz.models import Permission, Role, User
from modules.authz.permissions import ADMIN_ROLES, ADMIN_USERS
from modules.authz.service import (
    get_user_by_username,
    hash_password,
    is_admin_role,
    sync_admin_role_permissions,
    verify_password,
)
from modules.authz.ui_blocks import (
    UI_BLOCK_GROUPS,
    compute_ui_hidden_from_shown,
    ui_blocks_by_group,
    user_ui_hidden_set,
)
from modules.platform.business_domain import (
    UserViewScope,
    is_shared_scope_role_user,
    parse_user_view_scope,
    user_view_scope_choices,
)

router = APIRouter(prefix="/auth", tags=["auth"])
_MIN_PASSWORD_LENGTH = 8
KDS_SCOPE_OPTIONS = (
    ("ALL", "كل أقسام شاشة المطبخ"),
    ("CAFE", "المقهى / المشروبات فقط"),
    ("RESTAURANT", "المطعم / الوجبات فقط"),
)


def _all_permissions(db) -> list[Permission]:
    return list(db.scalars(select(Permission).order_by(Permission.id)).all())


def _users_page_perm_maps(users: list[User]) -> tuple[dict[int, list[int]], dict[int, list[int]], dict[int, list[str]]]:
    grant_ids: dict[int, list[int]] = {}
    deny_ids: dict[int, list[int]] = {}
    role_codes: dict[int, list[str]] = {}
    for u in users:
        grant_ids[u.id] = [p.id for p in (u.permission_grants or [])]
        deny_ids[u.id] = [p.id for p in (u.permission_denies or [])]
        role_codes[u.id] = sorted(
            {p.code for r in (u.roles or []) for p in (r.permissions or [])}
        )
    return grant_ids, deny_ids, role_codes


def _apply_user_permission_modes(
    db, user: User, *, form_get
) -> None:
    """form_get(name) -> value for perm_mode_{id}."""
    perms = _all_permissions(db)
    grants: list[Permission] = []
    denies: list[Permission] = []
    for p in perms:
        mode = (form_get(f"perm_mode_{p.id}") or "inherit").strip().lower()
        if mode == "grant":
            grants.append(p)
        elif mode == "deny":
            denies.append(p)
    user.permission_grants = grants
    user.permission_denies = denies


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    db: DBSession,
    username: str = Form(...),
    password: str = Form(...),
):
    user = get_user_by_username(db, username.strip())
    if user is None or not verify_password(password, user.password_hash):
        # استخدام 200 بدل 401/403 لتفادي ERR_INVALID_HTTP_RESPONSE في بعض المتصفحات مع نماذج HTML
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "اسم المستخدم أو كلمة المرور غير صحيحة."},
            status_code=status.HTTP_200_OK,
        )
    if not user.is_active:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "الحساب معطّل."},
            status_code=status.HTTP_200_OK,
        )
    request.session["user_id"] = user.id
    from modules.authz.kiosk import is_cashier_kiosk_user
    from modules.platform.business_domain import (
        is_hotel_scope_user,
        is_restaurant_scope_user,
    )

    if is_cashier_kiosk_user(user):
        return RedirectResponse("/pos", status_code=status.HTTP_302_FOUND)
    from modules.authz.domain_scope import default_landing_path

    if is_hotel_scope_user(user):
        from modules.authz.kiosk import requires_hotel_shift_pin

        if requires_hotel_shift_pin(user):
            return RedirectResponse("/admin/hotel/pin", status_code=status.HTTP_302_FOUND)
        return RedirectResponse(default_landing_path(user), status_code=status.HTTP_302_FOUND)
    if is_restaurant_scope_user(user):
        return RedirectResponse(default_landing_path(user), status_code=status.HTTP_302_FOUND)
    return RedirectResponse(default_landing_path(user), status_code=status.HTTP_302_FOUND)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=status.HTTP_302_FOUND)


# --- Admin: roles / users (mounted with prefix /admin in main) ---

admin_router = APIRouter(prefix="/admin", tags=["admin"])


@admin_router.post("/view-mode")
def admin_set_view_mode(
    request: Request,
    user: LoggedInUser,
    mode: str = Form("general"),
):
    from modules.platform.business_domain import (
        SESSION_VIEW_MODE_KEY,
        ViewMode,
        can_switch_view_mode,
        is_system_admin,
    )

    if not can_switch_view_mode(user):
        return RedirectResponse("/", status_code=302)
    raw = (mode or "").strip().lower()
    if not is_system_admin(user) and raw not in (
        ViewMode.RESTAURANT.value,
        ViewMode.HOTEL.value,
    ):
        raw = ViewMode.RESTAURANT.value
    try:
        request.session[SESSION_VIEW_MODE_KEY] = ViewMode(raw).value
    except ValueError:
        request.session[SESSION_VIEW_MODE_KEY] = (
            ViewMode.RESTAURANT.value if not is_system_admin(user) else ViewMode.GENERAL.value
        )
    ref = (request.headers.get("referer") or "/").strip()
    if not ref.startswith("/"):
        ref = "/"
    return RedirectResponse(ref, status_code=302)


@admin_router.get("/finance-hub", response_class=HTMLResponse)
def admin_finance_hub(
    request: Request,
    db: DBSession,
    user: LoggedInUser,
):
    from modules.authz.service import user_has_permission
    from modules.platform.business_domain import is_system_admin
    from modules.platform.finance_hub import build_finance_hub_context

    if not (
        is_system_admin(user)
        or user_has_permission(user, "reports:view")
        or user_has_permission(user, "gl:manage")
    ):
        return RedirectResponse("/", status_code=302)

    ctx = build_finance_hub_context(db, user, request.session)
    ctx["request"] = request
    return templates.TemplateResponse("admin_finance_hub.html", ctx)


@admin_router.get("/roles", response_class=HTMLResponse)
def admin_roles(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_ROLES)),
):
    roles = list(db.scalars(select(Role).order_by(Role.id)).all())
    return templates.TemplateResponse("admin_roles.html", {"request": request, "roles": roles})


@admin_router.get("/roles/{role_id}", response_class=HTMLResponse)
def admin_role_edit(
    request: Request,
    role_id: int,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_ROLES)),
):
    role = db.get(Role, role_id)
    if role is None:
        return RedirectResponse("/admin/roles", status_code=302)
    perms = list(db.scalars(select(Permission).order_by(Permission.code)).all())
    role_perm_ids = {p.id for p in role.permissions}
    return templates.TemplateResponse(
        "admin_role_edit.html",
        {
            "request": request,
            "role": role,
            "perms": perms,
            "role_perm_ids": role_perm_ids,
            "is_admin_role": is_admin_role(role),
        },
    )


@admin_router.post("/roles", response_class=HTMLResponse)
def admin_role_create(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_ROLES)),
    name_ar: str = Form(...),
):
    r = Role(name_ar=name_ar.strip())
    db.add(r)
    db.commit()
    return RedirectResponse(f"/admin/roles/{r.id}", status_code=302)


@admin_router.post("/roles/{role_id}", response_class=HTMLResponse)
def admin_role_save(
    request: Request,
    role_id: int,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_ROLES)),
    name_ar: str = Form(...),
    perm_ids: list[int] | None = Form(None),
):
    role = db.get(Role, role_id)
    if role is None:
        return RedirectResponse("/admin/roles", status_code=302)
    role.name_ar = name_ar.strip()
    if is_admin_role(role):
        sync_admin_role_permissions(db)
        db.commit()
        return RedirectResponse(f"/admin/roles/{role_id}?admin_locked=1", status_code=302)
    ids = perm_ids or []
    if ids:
        perms = db.execute(select(Permission).where(Permission.id.in_(ids))).scalars().all()
        role.permissions = list(perms)
    else:
        role.permissions = []
    db.commit()
    return RedirectResponse(f"/admin/roles/{role_id}", status_code=302)


def _transfer_wallets_for_admin(db):
    from modules.payments.models import PaymentMethodKind
    from modules.payments.service import list_payment_methods, payment_method_kind_matches

    return [
        m
        for m in list_payment_methods(db, only_active=True)
        if payment_method_kind_matches(m, PaymentMethodKind.CASH)
        or payment_method_kind_matches(m, PaymentMethodKind.BANK)
    ]


def _apply_user_wallet_access(db, user: User, form) -> None:
    from modules.authz.pos_wallet_access import replace_user_wallet_access

    send_ids = [int(x) for x in form.getlist("wallet_send") if str(x).isdigit()]
    recv_ids = [int(x) for x in form.getlist("wallet_recv") if str(x).isdigit()]
    replace_user_wallet_access(db, int(user.id), send_ids=send_ids, recv_ids=recv_ids)


def _admin_users_template_ctx(
    request: Request, db, *, users=None, roles=None, error: str | None = None
) -> dict:
    from modules.authz.pos_wallet_access import (
        default_clerk_send_method_ids,
        load_user_wallet_access_map,
    )

    users = users if users is not None else list(db.scalars(select(User).order_by(User.id)).all())
    roles = roles if roles is not None else list(db.scalars(select(Role).order_by(Role.id)).all())
    perms = _all_permissions(db)
    grant_ids, deny_ids, role_codes = _users_page_perm_maps(users)
    ctx = {
        "request": request,
        "users": users,
        "roles": roles,
        "perms": perms,
        "user_grant_ids": grant_ids,
        "user_deny_ids": deny_ids,
        "user_role_codes": role_codes,
        "kds_scope_options": KDS_SCOPE_OPTIONS,
        "view_scope_options": user_view_scope_choices(),
        "ui_block_groups": UI_BLOCK_GROUPS,
        "ui_blocks_by_group": ui_blocks_by_group(),
        "transfer_wallets": _transfer_wallets_for_admin(db),
        "default_send_ids": default_clerk_send_method_ids(db),
        "user_wallet_access": {u.id: load_user_wallet_access_map(db, u.id) for u in users},
    }
    if error:
        ctx["error"] = error
    return ctx


@admin_router.get("/users", response_class=HTMLResponse)
def admin_users(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_USERS)),
):
    return templates.TemplateResponse(
        "admin_users.html",
        _admin_users_template_ctx(request, db),
    )


@admin_router.post("/users", response_class=HTMLResponse)
async def admin_user_create(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_USERS)),
    username: str = Form(...),
    password: str = Form(...),
    kds_scope: str = Form("ALL"),
    view_scope: str = Form("both"),
    ui_show: list[str] | None = Form(None),
    pos_show_cash: str = Form(""),
    pos_show_bank: str = Form(""),
    role_ids: list[int] | None = Form(None),
):
    form = await request.form()
    users = list(db.scalars(select(User).order_by(User.id)).all())
    roles = list(db.scalars(select(Role).order_by(Role.id)).all())
    if len((password or "").strip()) < _MIN_PASSWORD_LENGTH:
        return templates.TemplateResponse(
            "admin_users.html",
            _admin_users_template_ctx(
                request,
                db,
                users=users,
                roles=roles,
                error=f"كلمة المرور يجب أن لا تقل عن {_MIN_PASSWORD_LENGTH} أحرف.",
            ),
            status_code=400,
        )
    if db.execute(select(User).where(User.username == username.strip())).scalar_one_or_none():
        return templates.TemplateResponse(
            "admin_users.html",
            _admin_users_template_ctx(
                request,
                db,
                users=users,
                roles=roles,
                error="اسم المستخدم موجود مسبقاً.",
            ),
            status_code=400,
        )
    scope = (kds_scope or "ALL").strip().upper()
    if scope not in {x[0] for x in KDS_SCOPE_OPTIONS}:
        scope = "ALL"
    parsed_view = parse_user_view_scope(view_scope)
    if parsed_view == UserViewScope.HOTEL:
        scope = "ALL"
    u = User(
        username=username.strip(),
        password_hash=hash_password(password),
        is_active=True,
        kds_scope=scope,
        view_scope=parsed_view.value,
        ui_hidden=compute_ui_hidden_from_shown(ui_show or []),
        pos_show_cash=pos_show_cash == "1",
        pos_show_bank=pos_show_bank == "1",
    )
    rids = role_ids or []
    if rids:
        u.roles = list(db.scalars(select(Role).where(Role.id.in_(rids))).all())
    if is_shared_scope_role_user(u):
        u.view_scope = UserViewScope.BOTH.value
    db.add(u)
    db.flush()
    _apply_user_permission_modes(db, u, form_get=lambda k: form.get(k))
    _apply_user_wallet_access(db, u, form)
    db.commit()
    return RedirectResponse("/admin/users", status_code=302)


@admin_router.post("/users/{user_id}/roles", response_class=HTMLResponse)
async def admin_user_roles_save(
    request: Request,
    user_id: int,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_USERS)),
    kds_scope: str = Form("ALL"),
    view_scope: str = Form("both"),
    ui_show: list[str] | None = Form(None),
    pos_show_cash: str = Form(""),
    pos_show_bank: str = Form(""),
    role_ids: list[int] | None = Form(None),
):
    form = await request.form()
    u = db.get(User, user_id)
    if u is None:
        return RedirectResponse("/admin/users", status_code=302)
    rids = role_ids or []
    u.roles = list(db.scalars(select(Role).where(Role.id.in_(rids))).all()) if rids else []
    parsed_view = parse_user_view_scope(view_scope)
    u.view_scope = (
        UserViewScope.BOTH.value
        if is_shared_scope_role_user(u)
        else parsed_view.value
    )
    u.ui_hidden = compute_ui_hidden_from_shown(ui_show or [])
    u.pos_show_cash = pos_show_cash == "1"
    u.pos_show_bank = pos_show_bank == "1"
    if parsed_view == UserViewScope.HOTEL:
        u.kds_scope = "ALL"
    else:
        scope = (kds_scope or "ALL").strip().upper()
        u.kds_scope = scope if scope in {x[0] for x in KDS_SCOPE_OPTIONS} else "ALL"
    _apply_user_permission_modes(db, u, form_get=lambda k: form.get(k))
    _apply_user_wallet_access(db, u, form)
    db.commit()
    return RedirectResponse("/admin/users", status_code=302)


@admin_router.post("/users/{user_id}/password", response_class=HTMLResponse)
def admin_user_password_save(
    user_id: int,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_USERS)),
    password: str = Form(...),
):
    u = db.get(User, user_id)
    if u is None:
        return RedirectResponse("/admin/users", status_code=302)
    new_password = (password or "").strip()
    if len(new_password) < _MIN_PASSWORD_LENGTH:
        return RedirectResponse(
            f"/admin/users?error=كلمة المرور يجب أن لا تقل عن {_MIN_PASSWORD_LENGTH} أحرف.",
            status_code=302,
        )
    u.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse("/admin/users", status_code=302)
