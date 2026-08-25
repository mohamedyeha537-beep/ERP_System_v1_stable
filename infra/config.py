from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(
        validation_alias=AliasChoices("DATABASE_URL", "database_url"),
    )
    # إعدادات تجميع الاتصالات — تُستخدم مع PostgreSQL على VPS
    db_pool_size: int = Field(
        default=5,
        validation_alias=AliasChoices("DB_POOL_SIZE", "db_pool_size"),
    )
    db_max_overflow: int = Field(
        default=10,
        validation_alias=AliasChoices("DB_MAX_OVERFLOW", "db_max_overflow"),
    )
    db_pool_pre_ping: bool = Field(
        default=True,
        validation_alias=AliasChoices("DB_POOL_PRE_PING", "db_pool_pre_ping"),
    )
    db_pool_recycle: int = Field(
        default=1800,
        validation_alias=AliasChoices("DB_POOL_RECYCLE", "db_pool_recycle"),
    )
    # يجب أن يكون عشوائياً وبطول ≥ 32 في الإنتاج
    secret_key: str = Field(
        min_length=32,
        validation_alias=AliasChoices("SECRET_KEY", "secret_key"),
    )
    session_cookie_name: str = Field(
        default="pos_session",
        validation_alias=AliasChoices("SESSION_COOKIE_NAME", "session_cookie_name"),
    )
    session_same_site: str = Field(
        default="lax",
        validation_alias=AliasChoices("SESSION_SAMESITE", "session_same_site"),
    )
    session_max_age: int = Field(
        default=86400,
        validation_alias=AliasChoices("SESSION_MAX_AGE", "session_max_age"),
    )
    session_https_only: bool = Field(
        default=False,
        validation_alias=AliasChoices("SESSION_HTTPS_ONLY", "session_https_only"),
    )
    # None يعطّل نقطة نهاية التكامل حتى يُضبط مفتاح قوي
    integration_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("INTEGRATION_API_KEY", "integration_api_key"),
    )
    # قائمة IP/CIDR مفصولة بفواصل — فارغة = لا قيد (للتطوير فقط)
    integration_api_ip_allowlist: str = Field(
        default="",
        validation_alias=AliasChoices(
            "INTEGRATION_API_IP_ALLOWLIST", "integration_api_ip_allowlist"
        ),
    )
    online_sync_ip_allowlist: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ONLINE_SYNC_IP_ALLOWLIST", "online_sync_ip_allowlist"
        ),
    )
    # عند true يُطلب توقيع HMAC-SHA256 للجسم (X-Sync-Signature)
    online_sync_require_hmac: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "ONLINE_SYNC_REQUIRE_HMAC", "online_sync_require_hmac"
        ),
    )
    # عند true تُرفض استعادة نسخة بلا ملف .sig صالح
    backup_require_signature: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "BACKUP_REQUIRE_SIGNATURE", "backup_require_signature"
        ),
    )
    default_admin_username: str = Field(
        default="admin",
        validation_alias=AliasChoices("DEFAULT_ADMIN_USERNAME", "default_admin_username"),
    )
    default_admin_password: str = Field(
        min_length=8,
        validation_alias=AliasChoices("DEFAULT_ADMIN_PASSWORD", "default_admin_password"),
    )
    # في الإنتاج اترك SEED_DEMO_USERS=false
    seed_demo_users: bool = Field(
        default=False,
        validation_alias=AliasChoices("SEED_DEMO_USERS", "seed_demo_users"),
    )
    demo_users_password: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DEMO_USERS_PASSWORD", "demo_users_password"),
    )
    show_docs: bool = Field(
        default=False,
        validation_alias=AliasChoices("SHOW_DOCS", "show_docs"),
    )
    app_env: str = Field(
        default="development",
        validation_alias=AliasChoices("APP_ENV", "app_env"),
    )
    # ========== مزامنة أوفلاين/أونلاين ==========
    sync_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("SYNC_ENABLED", "sync_enabled"),
    )
    sync_site_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SYNC_SITE_ID", "sync_site_id"),
    )
    online_sync_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ONLINE_SYNC_URL", "online_sync_url"),
    )
    online_sync_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ONLINE_SYNC_API_KEY", "online_sync_api_key"),
    )
    sync_interval_seconds: int = Field(
        default=60,
        validation_alias=AliasChoices("SYNC_INTERVAL_SECONDS", "sync_interval_seconds"),
    )
    sync_batch_size: int = Field(
        default=100,
        validation_alias=AliasChoices("SYNC_BATCH_SIZE", "sync_batch_size"),
    )
    sync_pull_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("SYNC_PULL_ENABLED", "sync_pull_enabled"),
    )

    @field_validator("database_url")
    @classmethod
    def _database_url_required(cls, value: str) -> str:
        url = (value or "").strip()
        if not url:
            raise ValueError(
                "DATABASE_URL مطلوب في ملف .env (مثال محلي: sqlite:///./pos.db)"
            )
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
