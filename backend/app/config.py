"""Environment-driven application settings.

See docs/ARCHITECTURE.md §6a for the pattern behind `enable_spreadsheet_import`.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


def as_sqlalchemy_url(url: str) -> str:
    """Render/Neon often supply `postgres://` or `postgresql://`.

    SQLAlchemy 2 with `psycopg` (v3) needs the `postgresql+psycopg` dialect
    or the first DB query raises and FastAPI returns a bare 500.
    """
    url = url.strip()
    if url.startswith(("postgres://", "postgresql://")) and not url.startswith(
        "postgresql+"
    ):
        _, rest = url.split("://", 1)
        return f"postgresql+psycopg://{rest}"
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./dev.db"
    jwt_secret: str = "change-me"
    enable_spreadsheet_import: bool = False
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    schwab_client_id: str = ""
    schwab_client_secret: str = ""
    schwab_redirect_uri: str = "https://127.0.0.1:8182"
    schwab_account_hash_rule1: str = ""
    schwab_account_hash_roth: str = ""

    @property
    def sqlalchemy_database_url(self) -> str:
        return as_sqlalchemy_url(self.database_url)

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
