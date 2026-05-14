"""فحص حالة الفئات والأصناف."""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import modules.authz.models  # noqa
import modules.catalog.models  # noqa
import modules.customers.models  # noqa
import modules.hotel.models  # noqa
import modules.hr.models  # noqa
import modules.inventory.models  # noqa
import modules.payments.models  # noqa
import modules.sales.models  # noqa
import modules.settings.models  # noqa

from infra.db import get_session_factory
from modules.catalog.models import Product, ProductCategory

S = get_session_factory()
db = S()
try:
    print("==== الفئات ====")
    for c in db.query(ProductCategory).order_by(ProductCategory.id).all():
        n = db.query(Product).filter(Product.category_id == c.id).count()
        print(f"  {c.id}: {c.name_ar}   parent={c.parent_id}   منتجات={n}")
    print("==== أصناف بلا فئة ====")
    for p in db.query(Product).filter(Product.category_id.is_(None)).all():
        print(f"  {p.id}: {p.name_ar}  (kind={p.kind.value})")
    print("==== أصناف فيها كلمة اختبار ====")
    for p in db.query(Product).filter(Product.name_ar.like("%اختبار%")).all():
        print(f"  {p.id}: {p.name_ar}  cat={p.category_id}")
finally:
    db.close()
