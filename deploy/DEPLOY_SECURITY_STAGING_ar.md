# نشر ترقيع الأمان على سيرفر التجريب (pos.baytak.ly)

بعد رفع الكوميت `3199e2a` إلى GitHub، نفّذ على الـ VPS:

```bash
# عدّل المسار إن كان مختلفاً عندك
cd /home/posbaytak/pos_app   # أو: /var/www/pos

git fetch origin
git checkout bayatak_hotel_roof_caffee
git pull --ff-only origin bayatak_hotel_roof_caffee

# إعدادات إنتاج/تجريب موصى بها (أضف لما ينقص في .env ثم احفظ)
# APP_ENV=production
# SESSION_HTTPS_ONLY=true
# SESSION_SAMESITE=strict
# ONLINE_SYNC_REQUIRE_HMAC=true
# BACKUP_REQUIRE_SIGNATURE=true
# INTEGRATION_API_IP_ALLOWLIST=...
# ONLINE_SYNC_IP_ALLOWLIST=...

sudo systemctl restart pos
sudo systemctl status pos --no-pager -l | head -n 30
```

## تحقق سريع بعد إعادة التشغيل

```bash
curl -sI https://pos.baytak.ly/auth/login | tr -d '\r' | grep -iE 'HTTP/|content-security|strict-transport|x-frame|x-content'
curl -s -o /dev/null -w "%{http_code}\n" https://pos.baytak.ly/auth/login
curl -s -o /dev/null -w "%{http_code}\n" https://pos.baytak.ly/uploads/sale_payments/no-such.jpg
curl -s -o /dev/null -w "%{http_code}\n" https://pos.baytak.ly/docs
```

توقّع بعد النشر:
- وجود `content-security-policy`
- وجود `strict-transport-security` (إن كان الطلب عبر HTTPS)
- `/auth/login` = 200
- `/uploads/sale_payments/...` بدون جلسة = 401 (أو 404 إن بقي nginx يخدم الملفات مباشرة — راجع إعداد nginx)
- `/docs` = 404

## ملاحظة nginx لـ /uploads

إذا كان nginx يخدم `/uploads` كـ `alias` مباشرة، لن تمر الحماية عبر التطبيق.
عطّل ذلك مؤقتاً أو وجّه الطلب إلى uvicorn:

```nginx
# احذف أو علّق location /uploads/ { alias ... }
# واترك الطلب يمر للـ proxy_pass الخاص بالتطبيق
```

ثم: `sudo nginx -t && sudo systemctl reload nginx`
