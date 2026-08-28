from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://finance:finance@db:5432/finance_sema"
    api_cors_origins: str = "http://localhost:3010"
    app_username: str = "admin"
    app_password: str = "finance"
    app_token_secret: str = "local-dev-secret"
    api_enable_docs: bool = False
    api_allowed_hosts: str = "*"
    login_max_attempts: int = 5
    login_lockout_seconds: int = 900
    # PDF attachments for asset costs are stored here as {cost.id}.pdf -
    # never under a user-supplied filename, to rule out path traversal. See
    # docker-compose*.yml for the volume mount that makes this persistent.
    attachments_dir: str = "/app/attachments"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.api_cors_origins.split(",") if origin.strip()]

    @property
    def allowed_hosts(self) -> list[str]:
        return [host.strip() for host in self.api_allowed_hosts.split(",") if host.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
