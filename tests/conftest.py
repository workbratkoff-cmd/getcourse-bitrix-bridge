from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app

SECRET = "test-secret"


@pytest.fixture
def client():
    """Приложение на :memory:-базе с выключенным воркером.

    Очередь в тестах прокручиваем вручную через drain(), чтобы не зависеть
    от таймингов фонового цикла.
    """
    settings = load_settings(
        port=0,
        db_path=":memory:",
        webhook_secret=SECRET,
        log_level="CRITICAL",
        bitrix={"mode": "stub"},
        whatsapp={"mode": "stub"},
        worker={
            "enabled": False,
            "max_attempts": 3,
            "backoff_base_sec": 0.001,
            "backoff_max_sec": 0.005,
        },
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.app_state = app.state
        yield test_client


def order_payload(**overrides) -> dict:
    """Пример из задания."""
    payload = {
        "event": "order.created",
        "order_id": "4817352",
        "cost": "24900",
        "offer": "Курс «Таргет с нуля»",
        "user": {
            "name": "Анна Петрова",
            "email": " Anna.Petrova@Mail.ru ",
            "phone": "8 (916) 123-45-67",
        },
    }
    payload.update(overrides)
    return payload


def webhook(client, payload=None, secret: str = SECRET, raw: bytes | None = None):
    url = f"/webhooks/getcourse?secret={secret}"
    if raw is not None:
        return client.post(url, content=raw, headers={"Content-Type": "application/json"})
    return client.post(url, json=payload)


def drain(client) -> dict:
    """Один проход очереди."""
    return asyncio.run(client.app_state.worker.tick())


def snapshot(client) -> dict:
    return client.app_state.store.snapshot()


def bitrix_methods(client) -> list[str]:
    return [call["method"] for call in client.app_state.bitrix.calls]


def inject_bitrix_failures(client, failures: int, error: Exception):
    """Подменяет BitrixClient.call так, чтобы первые `failures` вызовов падали."""
    bitrix = client.app_state.bitrix
    original = bitrix.call
    state = {"left": failures}

    async def failing_call(method, params):
        if state["left"] > 0:
            state["left"] -= 1
            raise error
        return await original(method, params)

    bitrix.call = failing_call
    return lambda: setattr(bitrix, "call", original)
