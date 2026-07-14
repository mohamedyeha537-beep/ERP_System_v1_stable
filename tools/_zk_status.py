from infra.background import with_db
from modules.settings.service import get_int, get_setting
from sqlalchemy import text
with with_db() as db:
    print("last_id", get_int(db, "zk_last_transaction_id", 0))
    print("last_sync", get_setting(db, "zk_last_sync_at", ""))
    rows = db.execute(
        text("SELECT zk_transaction_id, employee_id, action, detail FROM hr_zk_processed_punches WHERE zk_transaction_id BETWEEN 1 AND 10")
    ).fetchall()
    print("processed", rows)
