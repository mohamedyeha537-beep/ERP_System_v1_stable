"""تشغيل مهام خلفية (Fire-and-forget) خاصة بالعمليات الشبكية البطيئة.

الهدف
-----
عند نقطة البيع لا ينبغي أن يتأخّر الكاشير لأن الإنترنت بطيء. لذلك نضع كل
المكالمات الشبكية الاختيارية (WhatsApp, Email, Webhooks) في خيط منفصل
"daemon" يعمل بصمت في الخلفية بعد أن نُرجع استجابة فورية للمستخدم.

ضمانات السلامة
-------------
1. **سيشن مستقلة**: كل مهمة خلفية تأخذ سيشن SQLAlchemy جديدة خاصة بها
   عبر `with_db()` ـ لا نمرّر سيشن من الطلب الحالي (هذا يُفسد المعاملات).
2. **فشل صامت**: أي استثناء يُلتقط ويُسجّل في log فقط، لا يُسبّب تعطّلاً.
3. **خيط daemon**: لا يمنع إيقاف السيرفر لو كانت هناك مهمة معلّقة.
4. **حد أقصى وقت تشغيل**: تُلتزم المهام بـ timeout قصير لكل عملية شبكية
   (لا يمكن إجبار الخيط من الخارج، لكن العمليات نفسها تستخدم timeout).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

log = logging.getLogger("background")


@contextmanager
def with_db():
    """يفتح سيشن SQLAlchemy جديدة، يُغلقها مهما حدث (للاستخدام داخل خيط)."""
    from infra.db import get_session_factory

    Session = get_session_factory()
    db = Session()
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def run_in_background(
    target: Callable[..., Any],
    *args: Any,
    name: str | None = None,
    **kwargs: Any,
) -> threading.Thread:
    """يشغّل دالة في خيط daemon، ويسجّل أي استثناء بدلاً من رفعه.

    يعيد كائن الخيط ولكن لا يجب الانتظار عليه؛ هي fire-and-forget.

    >>> def slow_call(x): ...
    >>> run_in_background(slow_call, 42, name="alerts")
    """

    def _runner():
        try:
            target(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("background task '%s' failed: %s", name or target.__name__, exc)

    th = threading.Thread(
        target=_runner,
        name=f"bg-{name or target.__name__}",
        daemon=True,
    )
    th.start()
    return th
