from typing import Annotated

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from infra.db import get_db as infra_get_db
from modules.authz.models import User
from modules.authz.service import user_has_permission


def get_db_session():
    yield from infra_get_db()


DBSession = Annotated[Session, Depends(get_db_session)]


def get_current_user(request: Request, db: DBSession) -> User | None:
    """يعيد المستخدم مربوطاً بجلسة هذا الطلب — لا نعيد كائن الكاش المنفصل."""
    uid = None
    if getattr(request.state, "_user_attached", False):
        cached = getattr(request.state, "current_user", None)
        if cached is None:
            return None
        uid = getattr(cached, "id", None)
    if uid is None:
        raw = request.session.get("user_id") if hasattr(request, "session") else None
        if not raw:
            return None
        uid = int(raw)
    user = db.get(User, int(uid))
    if user is None or not user.is_active:
        if not getattr(request.state, "_user_attached", False):
            request.session.clear()
        return None
    request.state.current_user = user
    return user


CurrentUser = Annotated[User | None, Depends(get_current_user)]


def require_login(user: CurrentUser) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/auth/login"},
        )
    return user


LoggedInUser = Annotated[User, Depends(require_login)]


def require_permission(code: str):
    def dep(user: User = Depends(require_login)) -> User:
        if not user_has_permission(user, code):
            raise HTTPException(status_code=403, detail="ليس لديك صلاحية لهذا الإجراء.")
        return user

    return dep


def require_any_permission(*codes: str) -> Callable[..., User]:
    def dep(user: User = Depends(require_login)) -> User:
        if any(user_has_permission(user, code) for code in codes):
            return user
        raise HTTPException(status_code=403, detail="ليس لديك صلاحية لهذا الإجراء.")

    return dep


def require_module(module_key: str):
    """يتحقق من تفعيل الوحدة (خطط الاشتراك) — يتطلب DB في الطلب."""

    def dep(request: Request, db: DBSession) -> None:
        from modules.platform.module_registry import is_module_enabled

        if not is_module_enabled(db, module_key):
            raise HTTPException(status_code=403, detail="هذه الوحدة غير مفعّلة في خطتك.")

    return dep
