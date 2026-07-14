"""إعادة إرسال إشعارات فاشلة — python -m modules.notifications.retry_cli"""
from __future__ import annotations

from infra.db import get_session_factory
from modules.notifications.service import NotificationService


def main() -> None:
    Session = get_session_factory()
    db = Session()
    try:
        n = NotificationService.retry_failed(db, limit=100)
        db.commit()
        print(f"Retried {n} notification(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
