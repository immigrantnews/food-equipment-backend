from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="/root/food-equipment-backend/.env", extra="ignore")

    anthropic_api_key: str = ""

    anthropic_model: str = "claude-sonnet-4-6"

    telegram_bot_token: str = ""
    telegram_notify_token: str = ""
    telegram_chat_id: str = ""
    telegram_group_chat_id: int = 0

    tinkoff_terminal_key: str = ""
    tinkoff_secret_key: str = ""

    gemini_api_key: str = ""

    cors_origins: str = "*"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
