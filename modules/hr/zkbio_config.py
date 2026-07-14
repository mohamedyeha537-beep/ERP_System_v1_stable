"""Read ZKBioTime PostgreSQL connection settings (attsite.ini + POS settings)."""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from modules.settings.service import get_int, get_setting

DEFAULT_INI = Path(r"C:\ZKBioTime\attsite.ini")


@dataclass(frozen=True)
class ZkBioDbConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str

    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password} connect_timeout=5"
        )


def zkbio_install_detected() -> bool:
    """ZKBioTime مثبت على نفس جهاز Windows (مسار attsite.ini)."""
    return DEFAULT_INI.is_file()


def read_attsite_ini(path: Path | None = None) -> ZkBioDbConfig | None:
    ini_path = path or DEFAULT_INI
    if not ini_path.is_file():
        return None
    cp = configparser.ConfigParser()
    try:
        cp.read(ini_path, encoding="utf-8")
    except configparser.Error:
        return None
    if "DATABASE" not in cp:
        return None
    sec = cp["DATABASE"]
    if (sec.get("ENGINE") or "").strip().lower() != "postgresql":
        return None
    try:
        port = int((sec.get("PORT") or "7496").strip())
    except ValueError:
        port = 7496
    return ZkBioDbConfig(
        host=(sec.get("HOST") or "127.0.0.1").strip(),
        port=port,
        dbname=(sec.get("NAME") or "biotime").strip(),
        user=(sec.get("USER") or "postgres").strip(),
        password=(sec.get("PASSWORD") or "").strip(),
    )


def zkbio_settings_configured(db: Session) -> bool:
    """إعدادات اتصال ZKBioTime محفوظة في POS (للسيرفر اللايف)."""
    host = (get_setting(db, "zk_biotime_host") or "").strip()
    if host:
        return True
    if (os.getenv("ZK_BIOTIME_PASSWORD") or "").strip():
        return True
    return zkbio_install_detected()


def get_zkbio_db_config(db: Session) -> ZkBioDbConfig | None:
    """Merge POS settings with attsite.ini (password usually from ini only)."""
    ini = read_attsite_ini()
    host = (get_setting(db, "zk_biotime_host") or "").strip()
    if not host:
        if ini is not None:
            host = ini.host
        else:
            return None
    port = get_int(db, "zk_biotime_port", ini.port if ini else 7496)
    dbname = (get_setting(db, "zk_biotime_db") or "").strip()
    if not dbname:
        dbname = (ini.dbname if ini else "") or "biotime"
    user = (get_setting(db, "zk_biotime_user") or "").strip()
    if not user:
        user = (ini.user if ini else "") or "postgres"
    password = (get_setting(db, "zk_biotime_password") or "").strip()
    if not password and ini is not None:
        password = ini.password
    if not password:
        env_pwd = (os.getenv("ZK_BIOTIME_PASSWORD") or "").strip()
        if env_pwd:
            password = env_pwd
    if not host or not dbname or not user:
        return None
    return ZkBioDbConfig(
        host=host,
        port=max(1, port),
        dbname=dbname,
        user=user,
        password=password,
    )
