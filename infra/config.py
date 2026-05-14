from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(
        default="sqlite:///./pos.db",
        validation_alias=AliasChoices("DATABASE_URL", "database_url"),
    )
    secret_key: str = Field(
        default="change-me-in-production-use-long-random-string",
        validation_alias=AliasChoices("SECRET_KEY", "secret_key"),
    )
    session_cookie_name: str = Field(
        default="pos_session",
        validation_alias=AliasChoices("SESSION_COOKIE_NAME", "session_cookie_name"),
    )
    integration_api_key: str = Field(
        default="dev-integration-key-change-me",
        validation_alias=AliasChoices("INTEGRATION_API_KEY", "integration_api_key"),
    )
    default_admin_username: str = Field(
        default="admin",
        validation_alias=AliasChoices("DEFAULT_ADMIN_USERNAME", "default_admin_username"),
    )
    default_admin_password: str = Field(
        default="admin123",
        validation_alias=AliasChoices("DEFAULT_ADMIN_PASSWORD", "default_admin_password"),
    )
    seed_demo_users: bool = Field(
        default=True,
        validation_alias=AliasChoices("SEED_DEMO_USERS", "seed_demo_users"),
    )
    demo_users_password: str = Field(
        default="demo123",
        validation_alias=AliasChoices("DEMO_USERS_PASSWORD", "demo_users_password"),
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
