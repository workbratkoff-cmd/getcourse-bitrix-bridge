"""Настройки: переменные окружения и .env."""
from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BitrixSettings(BaseModel):
    # stub — только пишем в лог; webhook — реально ходим в портал
    mode: str = "stub"
    webhook_url: str = ""
    stage_new: str = "NEW"
    stage_paid: str = "WON"
    category_id: str = "0"
    timeout_sec: float = 10.0


class WhatsAppSettings(BaseModel):
    mode: str = "stub"


class WorkerSettings(BaseModel):
    enabled: bool = True
    poll_interval_sec: float = 1.0
    batch_size: int = 10
    max_attempts: int = 8
    backoff_base_sec: float = 2.0
    backoff_max_sec: float = 300.0


class Settings(BaseSettings):
    # вложенные секции задаются двойным подчёркиванием: BITRIX__MODE=webhook

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    port: int = 3000
    host: str = "0.0.0.0"
    webhook_secret: str = "dev-secret"
    db_path: str = "./data/app.db"
    default_country_code: str = "7"
    log_level: str = "INFO"

    bitrix: BitrixSettings = Field(default_factory=BitrixSettings)
    whatsapp: WhatsAppSettings = Field(default_factory=WhatsAppSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)

    def model_post_init(self, __context) -> None:
        self.bitrix.webhook_url = self.bitrix.webhook_url.rstrip("/")
        if self.bitrix.mode == "webhook" and not self.bitrix.webhook_url:
            raise ValueError("BITRIX__MODE=webhook требует заполненный BITRIX__WEBHOOK_URL")


def load_settings(**overrides) -> Settings:
    # overrides нужны тестам и демо-скрипту
    return Settings(**overrides)
