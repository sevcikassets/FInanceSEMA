from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://finance:finance@db:5432/finance_sema"
    api_cors_origins: str = "http://localhost:3010"
    app_username: str = "admin"
    app_password: str = "finance"
    app_token_secret: str = "local-dev-secret"
    # PDF attachments for asset costs are stored here as {cost.id}.pdf -
    # never under a user-supplied filename, to rule out path traversal. See
    # docker-compose*.yml for the volume mount that makes this persistent.
    attachments_dir: str = "/app/attachments"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.api_cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
