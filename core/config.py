from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Telegram Bot API accepts uploads up to 50 MB from bots; keep a small safety
# margin and clamp (rather than crash) when operators configure a higher value.
_MAX_FILE_MB_CAP = 49
_MAX_CAROUSEL_MB_CAP = 500


class Settings(BaseSettings):
    bot_token: SecretStr
    admin_ids: str = ""
    database_url: str = "sqlite+aiosqlite:///./data/bot.sqlite3"
    max_file_mb: int = Field(default=_MAX_FILE_MB_CAP, ge=1)
    max_carousel_mb: int = Field(default=100, ge=1)
    history_retention_days: int = Field(default=30, ge=1, le=3650)

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("max_file_mb", mode="before")
    @classmethod
    def _clamp_file_mb(cls, value):
        return cls._clamp(value, _MAX_FILE_MB_CAP)

    @field_validator("max_carousel_mb", mode="before")
    @classmethod
    def _clamp_carousel_mb(cls, value):
        return cls._clamp(value, _MAX_CAROUSEL_MB_CAP)

    @staticmethod
    def _clamp(value, cap: int):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return value  # defer to pydantic's own type error
        return min(number, cap)

    @property
    def admin_id_set(self) -> frozenset[int]:
        result: set[int] = set()
        for raw in self.admin_ids.split(","):
            raw = raw.strip()
            if raw:
                try:
                    result.add(int(raw))
                except ValueError as exc:
                    raise ValueError("ADMIN_IDS must be comma-separated Telegram numeric IDs") from exc
        return frozenset(result)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
