"""إعدادات مشتركة للاختبارات — لا تفعّل Secure cookies على TestClient (HTTP)."""
from __future__ import annotations

import os

# بيئة الاختبار محلية عبر HTTP؛ SESSION_HTTPS_ONLY=true يكسر الجلسة في TestClient.
# تفعيل Secure للكوكي يتم على سيرفر TEST (HTTPS) فقط عبر deploy script.
os.environ["SESSION_HTTPS_ONLY"] = "false"
