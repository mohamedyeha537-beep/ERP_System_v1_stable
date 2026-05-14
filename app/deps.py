from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from infra.db import get_db as infra_get_db
from modules.authz.models import User
from modules.authz.service import user_has_permission


def get_db_session():
    yield from infra_get_db()


DBSession = Annotated[Session, Depends(get_db_session)]


def get_current_user(request: Request, db: DBSession) -> User | None:
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = db.get(User, int(uid))
    if user is None or not user.is_active:
        request.session.clear()
        return None
    db.refresh(user, ["roles"])
    for r in user.roles:
        db.refresh(r, ["permissions"])
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
