from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    WEBHOOK_SECRET: str
    PAPER_MODE: bool = True
    RISK_PERCENT: float = 0.7
    DEFAULT_LEVERAGE: int = 5
    MAX_POSITIONS: int = 5

    # Te pola zostawiamy opcjonalne (na przyszłość)
    API_KEY: str | None = None
    API_SECRET: str | None = None
    TESTNET: bool = True
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CHAT_ID: str | None = None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

@lru_cache()
def get_settings() -> Settings:
    return Settings()
