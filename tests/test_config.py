import pytest
from pydantic import ValidationError

from app.config import Settings

PRODUCTION_OK = {
    "env": "production",
    "google_mode": "real",
    "dev_login": False,
    "storage_backend": "r2",
    "secret_key": "a-real-secret",
    "app_base_url": "https://app.example.com",
    "google_oauth_client_id": "id.apps.googleusercontent.com",
    "google_oauth_client_secret": "secret",
    "google_service_account_json": "e30=",
    "google_dwd_client_id": "123",
}


def make(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_production_accepts_safe_configuration():
    assert make(**PRODUCTION_OK).is_production


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"google_mode": "fake"}, "GOOGLE_MODE"),
        ({"dev_login": True}, "DEV_LOGIN"),
        ({"storage_backend": "local"}, "STORAGE_BACKEND"),
        ({"google_oauth_client_id": ""}, "GOOGLE_OAUTH_CLIENT_ID"),
        ({"google_oauth_client_secret": ""}, "GOOGLE_OAUTH_CLIENT_ID"),
        ({"google_service_account_json": ""}, "GOOGLE_SERVICE_ACCOUNT_JSON"),
        ({"google_dwd_client_id": ""}, "GOOGLE_DWD_CLIENT_ID"),
    ],
)
def test_production_rejects_development_switches(override, expected):
    with pytest.raises(ValidationError, match=expected):
        make(**{**PRODUCTION_OK, **override})


def test_production_rejects_default_secret_key(monkeypatch):
    monkeypatch.delenv("SECRET_KEY")
    config = {k: v for k, v in PRODUCTION_OK.items() if k != "secret_key"}
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        make(**config)


def test_development_allows_fake_mode():
    settings = make(env="dev", google_mode="fake", dev_login=True, storage_backend="local")
    assert not settings.is_production


@pytest.mark.parametrize(
    "url",
    [
        "postgres://u:p@host:5432/db",
        "postgresql://u:p@host:5432/db",
        "postgresql+psycopg://u:p@host:5432/db",
    ],
)
def test_database_url_forms(url):
    settings = make(database_url=url)
    assert settings.database_dsn == "postgresql://u:p@host:5432/db"
    assert settings.sqlalchemy_url == "postgresql+psycopg://u:p@host:5432/db"


def test_public_urls_fall_back_to_app_base_url():
    settings = make(app_base_url="https://app.example.com/", tracking_base_url="https://t.co/")
    assert settings.app_base_url == "https://app.example.com"
    assert settings.marketing_base_url == "https://app.example.com"
    assert settings.asset_base_url == "https://app.example.com"
    assert settings.tracking_base_url == "https://t.co"
