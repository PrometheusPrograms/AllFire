"""Environment-driven application settings.

See docs/ARCHITECTURE.md §6a for the pattern behind `enable_spreadsheet_import`.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./dev.db"
    jwt_secret: str = "change-me"
    enable_spreadsheet_import: bool = False
    cors_origins: str = "http://localhost:3000"
    schwab_client_id: str = ""
    schwab_client_secret: str = ""
    schwab_redirect_uri: str = "https://127.0.0.1:8182"
    schwab_account_hash_rule1: str = ""
    schwab_account_hash_roth: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
