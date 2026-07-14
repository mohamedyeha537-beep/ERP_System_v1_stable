#!/bin/bash
# إصلاح مخطط MySQL على VPS — شغّل من مجلد المشروع:
#   cd /var/www/pos && bash deploy/fix-live-schema.sh
set -euo pipefail
cd "$(dirname "$0")/.."
echo "==> POS schema fix"
if [ ! -d .venv ]; then
  echo "ERROR: .venv not found. Run from project root on VPS."
  exit 1
fi
source .venv/bin/activate
python tools/fix_mysql_catalog_schema.py
echo "==> Restarting pos service (if installed)"
if systemctl is-active --quiet pos 2>/dev/null; then
  sudo systemctl restart pos
  echo "pos service restarted"
else
  echo "No systemd pos service — restart uvicorn manually"
fi
echo "Done. Test: /catalog/products/new"
