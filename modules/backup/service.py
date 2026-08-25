"""خدمة النسخ الاحتياطي / الاستعادة / تصفير قاعدة البيانات.

مستويات التصفير:
1) `clear_transactions` — يحذف الحركات التشغيلية (مبيعات، جلسات، خزينة، مشتريات بضائع،
   مصروفات، حركات مخزون، مرتجعات، توصيل، حجوزات الفندق، ورديات الاستقبال، …).
   يُبقي الإعدادات والكتالوج والمستخدمين وسجل الأصول الثابتة (للإهلاك)
   مع تصفير أرصدة الخزينة والمخزون.
2) `clear_all_except_users` — يحذف كل البيانات التشغيلية مع الكتالوج والإعدادات،
   ويُبقي فقط: المستخدمون، الأدوار، الصلاحيات (ضرورية للدخول).
3) `factory_reset` — يحذف ملف قاعدة البيانات بالكامل (يتم على القرص).
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from infra.config import get_settings
from infra.database import (
    database_kind,
    database_label_ar,
    is_mysql_url,
    is_postgresql_url,
    is_server_database_url,
    is_sqlite_url,
    server_backup_suffix as backup_suffix_for_url,
)
from infra.db import get_engine


# ===== مساعدات الموقع =====


def _sqlite_path() -> Path | None:
    """يستخرج مسار ملف SQLite من DATABASE_URL إن كان sqlite، وإلا None."""
    url = get_settings().database_url or ""
    if not is_sqlite_url(url):
        return None
    # sqlite:///./pos.db  أو  sqlite:////absolute/path/db
    raw = url.split("sqlite:///", 1)[-1]
    if not raw or raw == ":memory:":
        return None
    p = Path(raw).resolve()
    return p


def is_sqlite() -> bool:
    return _sqlite_path() is not None


def is_postgresql() -> bool:
    return is_postgresql_url(get_settings().database_url or "")


def is_mysql() -> bool:
    return is_mysql_url(get_settings().database_url or "")


def is_server_database() -> bool:
    return is_server_database_url(get_settings().database_url or "")


def backup_file_suffix() -> str:
    return backup_suffix_for_url(get_settings().database_url or "")


def db_kind() -> str:
    return database_kind()


def db_kind_label_ar() -> str:
    return database_label_ar()


def db_path_str() -> str:
    if is_sqlite():
        p = _sqlite_path()
        return str(p) if p else "—"
    url = get_settings().database_url or ""
    # إخفاء كلمة المرور في العرض
    if "@" in url:
        head, tail = url.rsplit("@", 1)
        if ":" in head:
            scheme_user = head.rsplit(":", 1)[0]
            return f"{scheme_user}:****@{tail}"
    return url or "—"


def db_size_human() -> str:
    if is_sqlite():
        p = _sqlite_path()
        if p is None or not p.exists():
            return "—"
        sz = p.stat().st_size
    elif is_postgresql():
        try:
            eng = get_engine()
            with eng.connect() as conn:
                raw = conn.execute(
                    text("SELECT pg_database_size(current_database())")
                ).scalar()
            sz = int(raw or 0)
        except Exception:
            return "—"
    elif is_mysql():
        try:
            eng = get_engine()
            with eng.connect() as conn:
                raw = conn.execute(
                    text(
                        "SELECT COALESCE(SUM(data_length + index_length), 0) "
                        "FROM information_schema.tables "
                        "WHERE table_schema = DATABASE()"
                    )
                ).scalar()
            sz = int(raw or 0)
        except Exception:
            return "—"
    else:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if sz < 1024:
            return f"{sz:.1f} {unit}"
        sz /= 1024
    return f"{sz:.1f} TB"


def _backup_dir() -> Path:
    if is_sqlite():
        src = _sqlite_path()
        assert src is not None
        return src.parent / "backups"
    return Path.cwd() / "backups"


def _sqlalchemy_url():
    from sqlalchemy.engine import make_url

    return make_url(get_settings().database_url)


def _mysql_bin_candidates() -> list[Path]:
    """مسارات شائعة لمجلد mysql/bin (Windows/Linux) عندما لا يكون على PATH."""
    candidates: list[Path] = []
    env_bin = (os.environ.get("MYSQL_BIN") or os.environ.get("MYSQL_BIN_DIR") or "").strip()
    if env_bin:
        candidates.append(Path(env_bin))

    # Windows — XAMPP / Laragon / MySQL Installer
    for base in (
        Path(r"C:\xampp\mysql\bin"),
        Path(r"C:\XAMPP\mysql\bin"),
        Path(r"D:\xampp\mysql\bin"),
        Path(r"C:\laragon\bin\mysql"),
        Path(r"C:\Program Files\MySQL"),
        Path(r"C:\Program Files (x86)\MySQL"),
        Path(r"C:\Program Files\MariaDB"),
        Path(r"C:\Program Files (x86)\MariaDB"),
    ):
        if not base.exists():
            continue
        if (base / "mysqldump.exe").exists() or (base / "mysql.exe").exists():
            candidates.append(base)
            continue
        # MySQL Server 8.0\bin أو MariaDB 10.x\bin
        try:
            for child in sorted(base.iterdir(), reverse=True):
                bin_dir = child / "bin"
                if bin_dir.is_dir():
                    candidates.append(bin_dir)
        except OSError:
            pass

    # Linux / macOS
    for base in (
        Path("/usr/bin"),
        Path("/usr/local/bin"),
        Path("/usr/local/mysql/bin"),
        Path("/opt/homebrew/bin"),
        Path("/opt/mysql/bin"),
    ):
        if base.is_dir():
            candidates.append(base)

    return candidates


def _resolve_mysql_tool(tool: str) -> str:
    """يعيد مسار mysqldump أو mysql — من env أو PATH أو المواقع الشائعة."""
    tool = (tool or "").strip().lower()
    if tool not in ("mysqldump", "mysql"):
        raise ValueError(f"أداة MySQL غير معروفة: {tool}")

    env_key = "MYSQLDUMP_PATH" if tool == "mysqldump" else "MYSQL_PATH"
    explicit = (os.environ.get(env_key) or "").strip()
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)

    found = shutil.which(tool) or shutil.which(f"{tool}.exe")
    if found:
        return found

    exe_names = (f"{tool}.exe", tool)
    for bin_dir in _mysql_bin_candidates():
        for name in exe_names:
            candidate = bin_dir / name
            if candidate.is_file():
                return str(candidate)

    raise FileNotFoundError(tool)


@contextmanager
def _mysql_client_cmd(tool_bin: str):
    """يبني أمر mysql/mysqldump مع ملف إعدادات مؤقت لكلمة المرور (آمن على Linux)."""
    url = _sqlalchemy_url()
    extra_path: Path | None = None
    cmd = [tool_bin]
    try:
        if url.password:
            fd, extra_name = tempfile.mkstemp(prefix="pos-my-", suffix=".cnf")
            extra_path = Path(extra_name)
            pw = (url.password or "").replace("\\", "\\\\").replace('"', '\\"')
            user = (url.username or "root").replace('"', '\\"')
            host = (url.host or "127.0.0.1").replace('"', '\\"')
            port = int(url.port or 3306)
            body = (
                "[client]\n"
                f'user="{user}"\n'
                f'password="{pw}"\n'
                f'host="{host}"\n'
                f"port={port}\n"
            )
            os.write(fd, body.encode("utf-8"))
            os.close(fd)
            try:
                os.chmod(extra_path, 0o600)
            except OSError:
                pass
            # يجب أن يكون أول خيار بعد اسم الأداة
            cmd.append(f"--defaults-extra-file={extra_path}")
        else:
            cmd.extend(
                [
                    "-h",
                    url.host or "127.0.0.1",
                    "-P",
                    str(url.port or 3306),
                    "-u",
                    url.username or "root",
                ]
            )
        cmd.extend(
            [
                "--default-character-set=utf8mb4",
                "--max-allowed-packet=512M",
            ]
        )
        yield cmd, url
    finally:
        if extra_path is not None:
            try:
                extra_path.unlink()
            except OSError:
                pass


def _is_mysql_sql_file(path: Path) -> bool:
    name = (path.name or "").lower()
    return name.endswith(".sql") or name.endswith(".sql.gz") or name.endswith(".gz")


def _open_sql_text(path: Path):
    name = (path.name or "").lower()
    if name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


# ===== النسخ الاحتياطي =====


def _make_backup_sqlite(target_dir: Path) -> Path:
    src = _sqlite_path()
    if src is None:
        raise RuntimeError("ملف SQLite غير متاح.")
    if not src.exists():
        raise RuntimeError("ملف قاعدة البيانات غير موجود.")
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst = target_dir / f"pos-backup-{ts}.db"

    eng = get_engine()
    try:
        with eng.connect() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
    except Exception:
        pass

    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return dst


def _make_backup_postgresql(target_dir: Path) -> Path:
    import subprocess

    url = _sqlalchemy_url()
    if not url.drivername.startswith("postgres"):
        raise RuntimeError("DATABASE_URL ليس PostgreSQL.")

    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst = target_dir / f"pos-backup-{ts}.dump"

    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password

    cmd = [
        "pg_dump",
        "-h",
        url.host or "127.0.0.1",
        "-p",
        str(url.port or 5432),
        "-U",
        url.username or "postgres",
        "-Fc",
        "-f",
        str(dst),
        url.database or "postgres",
    ]
    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "أداة pg_dump غير موجودة. ثبّت PostgreSQL client على السيرفر."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"فشل pg_dump: {detail}") from exc
    return dst


def _make_backup_mysql(target_dir: Path) -> Path:
    import subprocess

    url = _sqlalchemy_url()
    if not url.drivername.startswith("mysql"):
        raise RuntimeError("DATABASE_URL ليس MySQL.")

    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst = target_dir / f"pos-backup-{ts}.sql"

    try:
        dump_bin = _resolve_mysql_tool("mysqldump")
    except FileNotFoundError as exc:
        raise RuntimeError(
            "أداة mysqldump غير موجودة. ثبّت MySQL client أو عيّن MYSQL_BIN "
            r"(مثال Windows: C:\xampp\mysql\bin) أو MYSQLDUMP_PATH."
        ) from exc

    try:
        with _mysql_client_cmd(dump_bin) as (cmd, url):
            cmd.extend(
                [
                    "--single-transaction",
                    "--routines",
                    "--triggers",
                ]
            )
            try:
                help_out = subprocess.run(
                    [dump_bin, "--help"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if "set-gtid-purged" in ((help_out.stdout or "") + (help_out.stderr or "")).lower():
                    cmd.append("--set-gtid-purged=OFF")
            except Exception:
                pass
            cmd.extend(
                [
                    "--result-file",
                    str(dst),
                    url.database or "pos_db",
                ]
            )
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True)
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "أداة mysqldump غير موجودة. ثبّت MySQL client أو عيّن MYSQL_BIN / MYSQLDUMP_PATH."
                ) from exc
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or str(exc)).strip()
                if "gtid" in detail.lower() or "set-gtid-purged" in detail.lower():
                    cmd = [c for c in cmd if c != "--set-gtid-purged=OFF"]
                    try:
                        subprocess.run(cmd, check=True, capture_output=True, text=True)
                    except subprocess.CalledProcessError as exc2:
                        detail2 = (exc2.stderr or exc2.stdout or str(exc2)).strip()
                        raise RuntimeError(f"فشل mysqldump: {detail2}") from exc2
                else:
                    raise RuntimeError(f"فشل mysqldump: {detail}") from exc
    except RuntimeError:
        raise
    return dst


def make_backup(target_dir: Path | None = None) -> Path:
    """ينشئ نسخة احتياطية ويعيد مسار الملف."""
    if target_dir is None:
        target_dir = _backup_dir()
    target_dir = Path(target_dir)
    if is_postgresql():
        return _make_backup_postgresql(target_dir)
    if is_mysql():
        return _make_backup_mysql(target_dir)
    if is_sqlite():
        return _make_backup_sqlite(target_dir)
    raise RuntimeError("نوع قاعدة البيانات غير مدعوم للنسخ الاحتياطي.")


def list_backups(target_dir: Path | None = None) -> list[tuple[str, int, datetime]]:
    """قائمة بالنسخ الاحتياطية: (اسم الملف، الحجم، تاريخ التعديل)."""
    if target_dir is None:
        target_dir = _backup_dir()
    if not target_dir.exists():
        return []
    rows = []
    patterns = (
        "pos-backup-*.db",
        "pos-backup-*.dump",
        "pos-backup-*.sql",
        "pos-backup-*.sql.gz",
    )
    seen: set[Path] = set()
    for pattern in patterns:
        for p in sorted(target_dir.glob(pattern), reverse=True):
            if p in seen:
                continue
            seen.add(p)
            st = p.stat()
            rows.append(
                (p.name, st.st_size, datetime.fromtimestamp(st.st_mtime, tz=timezone.utc))
            )
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows


def backup_path_for(name: str) -> Path | None:
    """يضمن أن الاسم آمن ويعود بالمسار الكامل لنسخة احتياطية."""
    target_dir = _backup_dir()
    candidate = (target_dir / name).resolve()
    if not str(candidate).startswith(str(target_dir.resolve())):
        return None
    if not candidate.exists():
        return None
    return candidate


# ===== الاستعادة =====


def _verify_sqlite_file(path: Path) -> tuple[bool, str]:
    """يتحقق أن الملف هو قاعدة SQLite صحيحة."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check")
            result = cur.fetchone()
            if not result or result[0] != "ok":
                return False, f"اختبار سلامة فشل: {result}"
            # تأكّد من وجود جداول معروفة
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users','sales','products')"
            )
            tables = {r[0] for r in cur.fetchall()}
            if not {"users", "products"}.intersection(tables):
                return (
                    False,
                    "هذا الملف لا يبدو وكأنه نسخة احتياطية صالحة لنظام نقطة البيع.",
                )
            return True, "OK"
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return False, f"الملف ليس قاعدة SQLite صحيحة: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"خطأ غير متوقع: {e}"


def restore_from_file(uploaded_path: Path) -> Path:
    """يستبدل قاعدة البيانات الحالية بنسخة احتياطية مرفوعة.

    يعمل: حفظ نسخة من الحالية كاحتياط، ثمّ نسخ المرفوع مكانها، ثمّ تحرير الاتصالات.
    """
    target = _sqlite_path()
    if target is None:
        raise RuntimeError("الاستعادة تعمل لقواعد SQLite فقط.")
    if not Path(uploaded_path).exists():
        raise RuntimeError("الملف المرفوع غير موجود.")

    ok, msg = _verify_sqlite_file(Path(uploaded_path))
    if not ok:
        raise RuntimeError(msg)

    # نسخة احتياطية أمان قبل الاستبدال
    safety_dir = target.parent / "backups"
    safety_dir.mkdir(parents=True, exist_ok=True)
    safety_name = (
        f"pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"
    )
    safety_path = safety_dir / safety_name
    if target.exists():
        shutil.copy2(target, safety_path)

    # تحرير اتصالات SQLAlchemy
    try:
        eng = get_engine()
        eng.dispose()
    except Exception:
        pass

    # استبدال الملف
    shutil.copy2(uploaded_path, target)

    # حذف ملفات WAL/SHM المتبقّية لضمان قراءة قاعدة جديدة نظيفة
    for ext in ("-wal", "-shm"):
        side = target.with_name(target.name + ext)
        try:
            if side.exists():
                side.unlink()
        except Exception:
            pass

    # إعادة إنشاء engine عبر first call
    from infra.db import get_session_factory

    get_session_factory()
    return safety_path


def _pg_connection_env():
    url = _sqlalchemy_url()
    if not url.drivername.startswith("postgres"):
        raise RuntimeError("DATABASE_URL ليس PostgreSQL.")
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password
    return env, url


def restore_from_dump(uploaded_path: Path) -> Path:
    """يستعيد PostgreSQL من ملف pg_dump (.dump).

    يأخذ نسخة أمان أولاً، ثم يُعيد بناء القاعدة عبر pg_restore.
    """
    import subprocess

    if not is_postgresql():
        raise RuntimeError("استعادة .dump تعمل مع PostgreSQL فقط.")
    if not uploaded_path.exists():
        raise RuntimeError("ملف الاستعادة غير موجود.")
    if uploaded_path.suffix.lower() != ".dump":
        raise RuntimeError("ملف PostgreSQL يجب أن ينتهي بـ .dump")

    safety_path = make_backup()
    env, url = _pg_connection_env()

    try:
        eng = get_engine()
        eng.dispose()
    except Exception:
        pass

    cmd = [
        "pg_restore",
        "-h",
        url.host or "127.0.0.1",
        "-p",
        str(url.port or 5432),
        "-U",
        url.username or "postgres",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "-d",
        url.database or "postgres",
        str(uploaded_path),
    ]
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "أداة pg_restore غير موجودة. ثبّت PostgreSQL client على السيرفر."
        ) from exc

    # pg_restore قد يُرجع 1 مع تحذيرات غير حرجة
    if result.returncode not in (0, 1):
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise RuntimeError(f"فشل pg_restore: {detail}")

    from infra.db import get_session_factory

    get_session_factory()
    return safety_path


# MySQL 8 → MariaDB / MySQL 5.7 (مثل XAMPP المحلي)
_MYSQL8_COLLATION_REWRITES: tuple[tuple[str, str], ...] = (
    ("utf8mb4_0900_ai_ci", "utf8mb4_unicode_ci"),
    ("utf8mb4_0900_as_ci", "utf8mb4_unicode_ci"),
    ("utf8mb4_0900_as_cs", "utf8mb4_bin"),
    ("utf8mb4_0900_bin", "utf8mb4_bin"),
    ("utf8mb3_0900_ai_ci", "utf8_unicode_ci"),
    ("utf8mb3_0900_as_ci", "utf8_unicode_ci"),
    ("utf8mb3_0900_as_cs", "utf8_bin"),
)


_DEFINER_RE = re.compile(r"DEFINER=`[^`]+`@`[^`]+`\s*", re.IGNORECASE)


def _rewrite_mysql8_sql_for_legacy(sql_text: str) -> str:
    """يستبدل collation MySQL 8 غير المدعوم في MariaDB/XAMPP."""
    out = sql_text
    for old, new in _MYSQL8_COLLATION_REWRITES:
        if old in out:
            out = out.replace(old, new)
    return out


def _sanitize_mysql_sql_text(sql_text: str) -> str:
    """إصلاح dump البرودكشن: collation + DEFINER + GTID (صلاحية SUPER)."""
    out = _rewrite_mysql8_sql_for_legacy(sql_text)
    if "DEFINER=" in out.upper() or "definer=" in out:
        out = _DEFINER_RE.sub("", out)
    if "GTID_PURGED" not in out.upper() and "SQL_LOG_BIN" not in out.upper():
        return out
    kept: list[str] = []
    for line in out.splitlines(keepends=True):
        head = line.lstrip().upper()
        if head.startswith("SET @@GLOBAL.GTID_PURGED") or head.startswith(
            "SET @@SESSION.SQL_LOG_BIN"
        ):
            continue
        kept.append(line)
    return "".join(kept)


def _iter_rewritten_sql_chunks(sql_path: Path, *, chunk_size: int = 1024 * 1024):
    """يقرأ ملف SQL (أو .gz) ويمرّر نصاً مُصلحاً على دفعات."""
    with _open_sql_text(sql_path) as sql_in:
        carry = ""
        while True:
            chunk = sql_in.read(chunk_size)
            if not chunk:
                if carry:
                    yield _sanitize_mysql_sql_text(carry)
                break
            data = carry + chunk
            nl = data.rfind("\n")
            if nl >= 0:
                yield _sanitize_mysql_sql_text(data[: nl + 1])
                carry = data[nl + 1 :]
            else:
                carry = data
                if len(carry) > 8 * 1024 * 1024:
                    yield _sanitize_mysql_sql_text(carry)
                    carry = ""


def _release_mysql_sessions_before_restore() -> None:
    """يقطع جلسات التطبيق على القاعدة — يمنع تعليق DROP TABLE أثناء الاستيراد."""
    try:
        eng = get_engine()
        eng.dispose()
    except Exception:  # noqa: BLE001
        pass
    if not is_mysql():
        return
    import subprocess

    try:
        mysql_bin = _resolve_mysql_tool("mysql")
        url = _sqlalchemy_url()
        db_name = (url.database or "pos_db").replace("`", "``")
        user_name = (url.username or "root").replace("'", "''")
        kill_query = (
            "SELECT id FROM information_schema.processlist "
            f"WHERE user = '{user_name}' AND id <> CONNECTION_ID()"
        )
        with _mysql_client_cmd(mysql_bin) as (cmd, _url):
            list_cmd = list(cmd) + ["-N", "-B", "-e", kill_query, url.database or "pos_db"]
            proc = subprocess.run(
                list_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            ids = [
                int(line.strip())
                for line in (proc.stdout or "").splitlines()
                if line.strip().isdigit()
            ]
            for pid in ids:
                with _mysql_client_cmd(mysql_bin) as (kcmd, _u2):
                    kill_cmd = list(kcmd) + ["-e", f"KILL {pid};", url.database or "pos_db"]
                    subprocess.run(
                        kill_cmd,
                        capture_output=True,
                        timeout=15,
                        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                    )
    except Exception:  # noqa: BLE001
        pass


def restore_from_sql(uploaded_path: Path) -> Path:
    """يستعيد MySQL من ملف mysqldump (.sql أو .sql.gz)."""
    import subprocess

    if not is_mysql():
        raise RuntimeError("استعادة .sql تعمل مع MySQL فقط.")
    if not uploaded_path.exists():
        raise RuntimeError("ملف الاستعادة غير موجود.")
    if not _is_mysql_sql_file(uploaded_path):
        raise RuntimeError("ملف MySQL يجب أن ينتهي بـ .sql أو .sql.gz")
    if uploaded_path.stat().st_size < 32:
        raise RuntimeError(
            "الملف فارغ أو لم يُرفع بالكامل. على السيرفر زد "
            "client_max_body_size في nginx إلى 256M ثم أعد التحميل."
        )

    safety_path = make_backup()

    _release_mysql_sessions_before_restore()

    try:
        mysql_bin = _resolve_mysql_tool("mysql")
    except FileNotFoundError as exc:
        raise RuntimeError(
            "أداة mysql غير موجودة. ثبّت MySQL client أو عيّن MYSQL_BIN "
            r"(مثال Windows: C:\xampp\mysql\bin) أو MYSQL_PATH."
        ) from exc

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        with _mysql_client_cmd(mysql_bin) as (cmd, url):
            cmd.append(url.database or "pos_db")
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            assert proc.stdin is not None
            try:
                for piece in _iter_rewritten_sql_chunks(uploaded_path):
                    proc.stdin.write(piece.encode("utf-8"))
                proc.stdin.close()
            except BrokenPipeError:
                pass
            except UnicodeEncodeError as exc:
                raise RuntimeError(
                    "فشل ترميز ملف SQL (يجب أن يكون UTF-8). أعد تصدير النسخة من البرودكشن بـ utf8mb4."
                ) from exc
            stdout_b, stderr_b = proc.communicate()
            result_code = proc.returncode
            result_err = (stderr_b or b"").decode("utf-8", errors="replace")
            result_out = (stdout_b or b"").decode("utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise RuntimeError(
            "أداة mysql غير موجودة. ثبّت MySQL client أو عيّن MYSQL_BIN / MYSQL_PATH."
        ) from exc

    if result_code != 0:
        detail = (result_err or result_out or f"exit {result_code}").strip()
        if "Unknown collation" in detail or "1273" in detail:
            detail += (
                " — تم تحويل collation MySQL 8 تلقائياً؛ إن استمر الخطأ "
                "حدّث MariaDB/XAMPP أو استورد على MySQL 8."
            )
        if "SUPER" in detail.upper() or "GTID" in detail.upper():
            detail += " — أُزيلت أوامر GTID/DEFINER تلقائياً؛ أعد المحاولة بعد رفع هذا التحديث."
        if "max_allowed_packet" in detail.lower() or "Got a packet bigger" in detail:
            detail += " — زد max_allowed_packet في MySQL إلى 512M."
        raise RuntimeError(f"فشل mysql restore: {detail}")

    _repatch_schema_after_restore()
    return safety_path


def _repatch_schema_after_restore() -> None:
    """بعد استعادة برودكشن قديمة: إعادة ترقيع الأعمدة الناقصة ثم تجديد الجلسات."""
    from infra.catalog_schema import repair_catalog_schema
    from infra.db import get_engine, get_session_factory
    from infra.schema_bootstrap import ensure_schema_patched, reset_schema_patch_flag

    try:
        eng = get_engine()
        eng.dispose()
    except Exception:
        pass
    reset_schema_patch_flag()
    ensure_schema_patched(force=True)
    try:
        repair_catalog_schema(get_engine())
    except Exception:
        pass
    get_session_factory()


def restore_backup(uploaded_path: Path) -> Path:
    """استعادة من نسخة احتياطية (.db / .dump / .sql)."""
    suffix = uploaded_path.suffix.lower()
    if is_sqlite():
        if suffix != ".db":
            raise RuntimeError("على SQLite استخدم ملف نسخة ينتهي بـ .db")
        path = restore_from_file(uploaded_path)
        _repatch_schema_after_restore()
        return path
    if is_postgresql():
        if suffix != ".dump":
            raise RuntimeError("على PostgreSQL استخدم ملف نسخة ينتهي بـ .dump")
        path = restore_from_dump(uploaded_path)
        _repatch_schema_after_restore()
        return path
    if is_mysql():
        if not _is_mysql_sql_file(uploaded_path):
            raise RuntimeError("على MySQL استخدم ملف نسخة ينتهي بـ .sql أو .sql.gz")
        return restore_from_sql(uploaded_path)
    raise RuntimeError("نوع قاعدة البيانات غير مدعوم للاستعادة.")


# جداول الحركات (transactions) — تُحذف عند مستوى 1
_TRANSACTION_TABLES = [
    "kitchen_tickets",
    "sale_payments",
    "sale_lines",
    "sales",
    "stock_movements",
    "inventory_lots",
    "purchase_lines",  # سيُحذف هنا فقط للبضائع والمصروفات
    "purchases",  # سيُحذف هنا فقط للبضائع والمصروفات
    "alerts_log",
]

# جداول الإعدادات/الكتالوج — تُحذف عند مستوى 2 (مع الحفاظ على المستخدمين)
_CONFIG_TABLES = [
    "bom_lines",
    "products",
    "product_categories",
    "product_units",
    "dining_tables",
    "recurring_costs",
    "alert_subscriptions",
    "low_stock_alerts",
    "app_settings",
    "payment_methods",
]


def _table_exists(db: Session, name: str) -> bool:
    insp = inspect(db.get_bind())
    return name in set(insp.get_table_names())


def _safe_delete(db: Session, sql: str) -> int:
    if "FROM " in sql:
        # استخراج اسم الجدول بشكل بسيط
        tbl = sql.split("FROM ", 1)[1].split()[0].strip().strip(";")
        if not _table_exists(db, tbl):
            return 0
    try:
        result = db.execute(text(sql))
        return int(result.rowcount or 0)
    except Exception:
        return 0


def _run_deletes(db: Session, counts: dict[str, int], steps: list[tuple[str, str]]) -> None:
    """ينفّذ DELETE بالترتيب ويسجّل عدد الصفوف المحذوفة."""
    for key, sql in steps:
        counts[key] = _safe_delete(db, sql)


def _clear_hotel_transactions(db: Session, counts: dict[str, int]) -> None:
    """محو حركات الفندق: حجوزات، ورديات، تحصيلات، فواتير، عجوزات، قيود GL."""
    _run_deletes(
        db,
        counts,
        [
            (
                "hr_deduction_repayments_hotel",
                "DELETE FROM hr_deduction_repayments WHERE deduction_id IN "
                "(SELECT id FROM hr_employee_deductions WHERE source_type IN "
                "('HOTEL_SHIFT_SHORTAGE', 'SHIFT_VARIANCE'))",
            ),
            (
                "hr_employee_deductions_hotel",
                "DELETE FROM hr_employee_deductions WHERE source_type IN "
                "('HOTEL_SHIFT_SHORTAGE', 'SHIFT_VARIANCE')",
            ),
            ("shift_variances", "DELETE FROM shift_variances"),
            ("shift_handovers", "DELETE FROM shift_handovers"),
            ("hotel_booking_payment_refunds", "DELETE FROM hotel_booking_payment_refunds"),
            ("hotel_booking_payments", "DELETE FROM hotel_booking_payments"),
            ("hotel_invoice_items", "DELETE FROM hotel_invoice_items"),
            ("hotel_invoices", "DELETE FROM hotel_invoices"),
            ("hotel_booking_debts", "DELETE FROM hotel_booking_debts"),
            ("hotel_booking_services", "DELETE FROM hotel_booking_services"),
            ("hotel_booking_service_rules", "DELETE FROM hotel_booking_service_rules"),
            ("hotel_booking_room_assignments", "DELETE FROM hotel_booking_room_assignments"),
            ("hotel_booking_guests", "DELETE FROM hotel_booking_guests"),
            ("hotel_booking_status_logs", "DELETE FROM hotel_booking_status_logs"),
            ("hotel_room_charges", "DELETE FROM hotel_room_charges"),
            ("hotel_bookings", "DELETE FROM hotel_bookings"),
            ("hotel_shifts", "DELETE FROM hotel_shifts"),
            ("hotel_daily_closings", "DELETE FROM hotel_daily_closings"),
            ("hotel_audit_logs", "DELETE FROM hotel_audit_logs"),
            ("hotel_room_status_logs", "DELETE FROM hotel_room_status_logs"),
            ("gl_journal_lines", "DELETE FROM gl_journal_lines"),
            ("gl_journal_entries", "DELETE FROM gl_journal_entries"),
            ("purchase_advances", "DELETE FROM purchase_advances"),
            ("treasury_sessions", "DELETE FROM treasury_sessions"),
            ("customer_wallet_transactions", "DELETE FROM customer_wallet_transactions"),
        ],
    )
    if _table_exists(db, "hotel_rooms"):
        try:
            db.execute(
                text(
                    "UPDATE hotel_rooms SET guest_name = NULL, physical_status = 'AVAILABLE'"
                )
            )
            counts["hotel_rooms_reset"] = 1
        except Exception:
            pass
    if _table_exists(db, "customers"):
        try:
            db.execute(
                text(
                    "UPDATE customers SET wallet_balance = 0, company_spendable_balance = 0"
                )
            )
            counts["customer_wallets_reset"] = 1
        except Exception:
            try:
                db.execute(text("UPDATE customers SET wallet_balance = 0"))
                counts["customer_wallets_reset"] = 1
            except Exception:
                pass


def _clear_notifications(db: Session, counts: dict[str, int]) -> None:
    """محو سجل الإشعارات وعداد الجرس (مركز النشاط + محرك الإشعارات)."""
    _run_deletes(
        db,
        counts,
        [
            ("notification_logs", "DELETE FROM notification_logs"),
            ("notification_actions", "DELETE FROM notification_actions"),
            ("notification_events", "DELETE FROM notification_events"),
            ("activity_hub_item_states", "DELETE FROM activity_hub_item_states"),
            ("dashboard_section_seen", "DELETE FROM dashboard_section_seen"),
            ("dashboard_activities", "DELETE FROM dashboard_activities"),
        ],
    )
    try:
        from modules.dashboard_notify.activity_hub import invalidate_bell_cache

        invalidate_bell_cache()
        counts["bell_cache_cleared"] = 1
    except Exception:
        pass


def clear_notifications(db: Session) -> dict[str, int]:
    """تصفير الإشعارات فقط — دون مسح العملاء أو الحركات."""
    counts: dict[str, int] = {}
    _clear_notifications(db, counts)
    db.commit()
    return counts


def clear_transactions(db: Session, *, keep_fixed_assets: bool = True) -> dict[str, int]:
    """مستوى 1: محو الحركات التشغيلية (مبيعات، جلسات، خزينة، مشتريات، مخزون، …).

    إن كان keep_fixed_assets=True فلن تُحذف فواتير Purchase(kind='ASSET') ولا بنودها
    (سجل الأصول الثابتة للإهلاك)، لكن تُحذف **كل** مدفوعات الشراء/التحويلات حتى
    تُصفَّر أرصدة الخزينة الرئيسية.

    ما يُبقى عمداً: الكتالوج، المستخدمون، الإعدادات، أساليب الدفع، الأصول الثابتة
    المسجَّلة (مع إهلاكها الشهري في التقارير)، وحدود إعادة الطلب في الكتالوج.
    """
    counts: dict[str, int] = {}

    # —— جلسات POS وعجز الكاشير ——
    _run_deletes(
        db,
        counts,
        [
            (
                "hr_deduction_repayments_shift",
                "DELETE FROM hr_deduction_repayments WHERE deduction_id IN "
                "(SELECT id FROM hr_employee_deductions WHERE source_type = 'POS_SHIFT_SHORTAGE')",
            ),
            (
                "hr_employee_deductions_shift",
                "DELETE FROM hr_employee_deductions WHERE source_type = 'POS_SHIFT_SHORTAGE'",
            ),
            ("pos_shift_shortages", "DELETE FROM pos_shift_shortages"),
            ("pos_shifts", "DELETE FROM pos_shifts"),
        ],
    )

    _clear_hotel_transactions(db, counts)

    # —— مرتجعات ومدفوعات مرتبطة بالمبيعات ——
    _run_deletes(
        db,
        counts,
        [
            ("refund_payments", "DELETE FROM refund_payments"),
            ("payment_transfers", "DELETE FROM payment_transfers"),
            ("sale_return_lines", "DELETE FROM sale_return_lines"),
            ("sale_returns", "DELETE FROM sale_returns"),
            ("delivery_cash_settlements", "DELETE FROM delivery_cash_settlements"),
            ("delivery_handoffs", "DELETE FROM delivery_handoffs"),
            ("kitchen_tickets", "DELETE FROM kitchen_tickets"),
            ("loyalty_transactions", "DELETE FROM loyalty_transactions"),
            ("referral_events", "DELETE FROM referral_events"),
            ("sale_payments", "DELETE FROM sale_payments"),
            ("sale_lines", "DELETE FROM sale_lines"),
            ("sales", "DELETE FROM sales"),
        ],
    )

    # —— مشتريات/مصروفات/مدفوعات (تصفير الخزينة) ——
    counts["purchase_payments"] = _safe_delete(db, "DELETE FROM purchase_payments")

    if keep_fixed_assets:
        counts["purchase_lines_inv_exp"] = _safe_delete(
            db,
            "DELETE FROM purchase_lines WHERE purchase_id IN "
            "(SELECT id FROM purchases WHERE kind IN ('INVENTORY','EXPENSE'))",
        )
        counts["purchases_inv_exp"] = _safe_delete(
            db, "DELETE FROM purchases WHERE kind IN ('INVENTORY','EXPENSE')"
        )
    else:
        counts["purchase_lines"] = _safe_delete(db, "DELETE FROM purchase_lines")
        counts["purchases"] = _safe_delete(db, "DELETE FROM purchases")

    counts["stock_movements"] = _safe_delete(db, "DELETE FROM stock_movements")

    # —— مخزون ——
    if _table_exists(db, "stock_balances"):
        try:
            db.execute(text("UPDATE stock_balances SET quantity = 0"))
            counts["stock_balances_zeroed"] = 1
        except Exception:
            pass
    if _table_exists(db, "products"):
        try:
            db.execute(text("UPDATE products SET stock_quantity = 0"))
            counts["products_stock_zeroed"] = 1
        except Exception:
            pass

    # —— ولاء العملاء (إحصائيات متراكمة من المبيعات) ——
    if _table_exists(db, "customers"):
        try:
            db.execute(
                text(
                    "UPDATE customers SET points_balance = 0, total_spent = 0, "
                    "visits_count = 0, last_visit_at = NULL"
                )
            )
            counts["customers_stats_reset"] = 1
        except Exception:
            pass

    # —— سجلات تشغيلية ثانوية ——
    _clear_notifications(db, counts)
    _run_deletes(
        db,
        counts,
        [
            ("print_jobs", "DELETE FROM print_jobs"),
            ("message_outbox", "DELETE FROM message_outbox"),
            ("alerts_log", "DELETE FROM alerts_log"),
        ],
    )

    if _table_exists(db, "app_settings"):
        try:
            db.execute(
                text(
                    "DELETE FROM app_settings WHERE key IN "
                    "('alerts_low_stock_fingerprint',)"
                )
            )
            counts["alert_fingerprints_cleared"] = 1
        except Exception:
            pass

    db.commit()
    return counts


def clear_all_except_users(db: Session) -> dict[str, int]:
    """مستوى 2: يحذف كل شيء عدا المستخدمين والأدوار والصلاحيات."""
    counts = clear_transactions(db, keep_fixed_assets=False)
    for tbl in _CONFIG_TABLES:
        counts[tbl] = _safe_delete(db, f"DELETE FROM {tbl}")
    db.commit()
    return counts


def factory_reset() -> dict[str, str]:
    """مستوى 3: حذف ملف قاعدة البيانات. يجب إعادة تشغيل الخادم بعدها.
    ينقل الملف الحالي إلى مجلد النسخ كاحتياط أخير قبل الحذف.
    """
    target = _sqlite_path()
    if target is None:
        raise RuntimeError("التصفير الكامل يعمل لقواعد SQLite فقط.")
    safety_dir = target.parent / "backups"
    safety_dir.mkdir(parents=True, exist_ok=True)
    archive = safety_dir / (
        "factory-reset-archive-"
        + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        + ".db"
    )

    try:
        eng = get_engine()
        eng.dispose()
    except Exception:
        pass

    if target.exists():
        shutil.move(str(target), str(archive))
    for ext in ("-wal", "-shm"):
        side = target.with_name(target.name + ext)
        try:
            if side.exists():
                side.unlink()
        except Exception:
            pass

    # إجبار إعادة إنشاء engine + ترحيل + بذور
    from infra.db import get_session_factory  # noqa
    from infra.sqlite_patch import patch_sqlite_schema

    eng = get_engine()  # سينشئ ملف جديد فارغ
    # نُنشئ كل الجداول من الـ models قبل تطبيق الـ patch (الـ patch يُكمل ما لم يكن موجوداً)
    from infra.db import Base

    Base.metadata.create_all(eng)
    patch_sqlite_schema(eng)

    return {
        "archived_to": str(archive),
        "new_db": str(target),
    }
