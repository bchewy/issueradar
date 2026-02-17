from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "GitHub Issues/PR Relevance Finder"
    app_version: str = "0.1.0"

    github_api_base: str = "https://api.github.com"
    github_timeout_seconds: float = 20.0
    github_retry_attempts: int = 2
    github_backoff_base_seconds: float = 0.5
    github_cache_ttl_seconds: int = 600
    github_comment_limit: int = 20
    github_query_max_chars: int = 256
    github_max_concurrency: int = 6

    llm_enabled: bool = True
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    llm_timeout_seconds: float = 45.0
    llm_cache_ttl_seconds: int = 3600
    llm_prompt_version: str = "v1"
    llm_max_body_chars: int = 2500
    llm_max_comment_chars: int = 700
    llm_comments_per_item: int = 3

    cache_max_entries: int = Field(default=4000, ge=100)

    github_client_id: str | None = None
    github_client_secret: str | None = None
    session_secret: str = "change-me-in-production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
