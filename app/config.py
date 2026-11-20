"""Application settings, read from environment variables (and `.env` in development)."""

from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET_KEY = "dev-only-insecure-secret-key"  # noqa: S105 - rejected in production below


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["dev", "production"] = "dev"
    log_level: str = "INFO"

    # Public URLs. The last three fall back to APP_BASE_URL when left empty.
    app_base_url: str = "http://127.0.0.1:8000"
    marketing_base_url: str = ""
    tracking_base_url: str = ""
    asset_base_url: str = ""

    secret_key: SecretStr = SecretStr(DEV_SECRET_KEY)
    # 127.0.0.1 rather than localhost: on Windows, localhost tries IPv6 first and stalls
    # against Docker's IPv4-only port mapping.
    database_url: str = "postgresql://footerpilot:footerpilot@127.0.0.1:5432/footerpilot"

    # Google
    google_mode: Literal["real", "fake"] = "fake"
    dev_login: bool = False
    google_oauth_client_id: str = ""
    google_oauth_client_secret: SecretStr = SecretStr("")
    google_service_account_json: SecretStr = SecretStr("")
    google_dwd_client_id: str = ""

    # Image storage
    storage_backend: Literal["r2", "local"] = "local"
    media_root: str = "var/media"  # folder used by the local backend
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: SecretStr = SecretStr("")
    r2_bucket: str = ""
    r2_public_base_url: str = ""

    # Billing
    razorpay_key_id: str = ""
    razorpay_key_secret: SecretStr = SecretStr("")
    razorpay_webhook_secret: SecretStr = SecretStr("")
    razorpay_plan_id: str = ""
    free_plan_max_employees: int = 5
    price_display: str = ""

    setup_call_url: str = ""
    support_email: str = ""
    sentry_dsn: str = ""

    @model_validator(mode="after")
    def _finalise(self) -> Self:
        self.app_base_url = self.app_base_url.rstrip("/")
        for name in ("marketing_base_url", "tracking_base_url", "asset_base_url"):
            value = getattr(self, name).rstrip("/")
            setattr(self, name, value or self.app_base_url)

        if self.env == "production":
            problems = []
            if self.google_mode == "fake":
                problems.append("GOOGLE_MODE must be 'real'")
            if self.dev_login:
                problems.append("DEV_LOGIN must be false")
            if self.storage_backend == "local":
                problems.append("STORAGE_BACKEND must be 'r2'")
            if self.secret_key.get_secret_value() == DEV_SECRET_KEY:
                problems.append("SECRET_KEY must be set")
            if not self.google_sign_in_configured:
                problems.append("GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET must be set")
            if problems:
                raise ValueError("Unsafe production configuration: " + "; ".join(problems))

        if self.google_mode == "real":
            missing = [
                name
                for name, value in (
                    (
                        "GOOGLE_SERVICE_ACCOUNT_JSON",
                        self.google_service_account_json.get_secret_value(),
                    ),
                    ("GOOGLE_DWD_CLIENT_ID", self.google_dwd_client_id),
                )
                if not value
            ]
            if missing:
                raise ValueError("GOOGLE_MODE=real requires " + " and ".join(missing))

        if self.storage_backend == "r2":
            missing = [
                name
                for name, value in (
                    ("R2_ACCOUNT_ID", self.r2_account_id),
                    ("R2_ACCESS_KEY_ID", self.r2_access_key_id),
                    ("R2_SECRET_ACCESS_KEY", self.r2_secret_access_key.get_secret_value()),
                    ("R2_BUCKET", self.r2_bucket),
                    ("R2_PUBLIC_BASE_URL", self.r2_public_base_url),
                )
                if not value
            ]
            if missing:
                raise ValueError("STORAGE_BACKEND=r2 requires " + ", ".join(missing))
        return self

    @property
    def google_sign_in_configured(self) -> bool:
        return bool(
            self.google_oauth_client_id and self.google_oauth_client_secret.get_secret_value()
        )

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def images_are_public(self) -> bool:
        """Whether images in signatures can be loaded by a mail client.

        False in local development: files stored on this machine, or served from a
        loopback address, cannot be reached by Gmail.
        """
        host = (urlsplit(self.asset_base_url).hostname or "").lower()
        loopback = host in ("localhost", "testserver", "::1") or host.startswith("127.")
        return self.storage_backend == "r2" and not loopback

    @property
    def database_dsn(self) -> str:
        """libpq-style URL, used by psycopg and Procrastinate."""
        _, _, rest = self.database_url.partition("://")
        return f"postgresql://{rest}"

    @property
    def sqlalchemy_url(self) -> str:
        """Same database through SQLAlchemy's psycopg 3 dialect (sync and async)."""
        _, _, rest = self.database_url.partition("://")
        return f"postgresql+psycopg://{rest}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
