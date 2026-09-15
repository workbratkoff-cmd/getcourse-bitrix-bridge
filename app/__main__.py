"""Точка входа: python -m app"""
from __future__ import annotations

import uvicorn

from .config import load_settings
from .main import create_app

settings = load_settings()

uvicorn.run(
    create_app(settings),
    host=settings.host,
    port=settings.port,
    log_config=None,  # логирование настраивается в app.logger
)
