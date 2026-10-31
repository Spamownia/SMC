from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    WEBHOOK_SECRET: str
    API_KEY: str
    API_SECRET: str
    TESTNET: bool = True
    DEFAULT_LEVERAGE: int = 5
    RISK_PERCENT: float = 0.7
    MAX_POSITIONS: int = 3
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CHAT_ID: str | None = None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

@lru_cache()
def get_settings() -> Settings:
    return Settings()
