from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    airtable_token: str = ""
    airtable_base_id: str = ""

    airtable_leads_table: str = "Leads"
    airtable_listings_table: str = "Listings"
    airtable_want_to_buy_table: str = "WantToBuy"

    anthropic_model: str = "claude-sonnet-4-6"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    cors_origins: str = "*"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
