"""Сквозная демонстрация без внешних систем.

Поднимает сервис на базе в памяти, прогоняет заказ из задания через
order.created -> повторную доставку -> order.paid и печатает, что оказалось
в базе и какие вызовы ушли бы в Bitrix24.

    python scripts/demo.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.main import create_app  # noqa: E402

SECRET = "demo-secret"

settings = load_settings(
    db_path=":memory:",
    webhook_secret=SECRET,
    log_level="INFO",
    bitrix={"mode": "stub"},
    whatsapp={"mode": "stub"},
    worker={"enabled": False},
)
app = create_app(settings)


def payload(event: str) -> dict:
    return {
        "event": event,
        "order_id": "4817352",
        "cost": "24900",
        "offer": "Курс «Таргет с нуля»",
        "user": {
            "name": "Анна Петрова",
            "email": " Anna.Petrova@Mail.ru ",
            "phone": "8 (916) 123-45-67",
        },
    }


def step(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


with TestClient(app) as client:
    def post(event: str) -> None:
        response = client.post(f"/webhooks/getcourse?secret={SECRET}", json=payload(event))
        print(f"\n--- POST {event} -> HTTP {response.status_code}")
        dump(response.json())

    step("1. Приходит order.created")
    post("order.created")
    asyncio.run(app.state.worker.tick())

    step("2. Тот же вебхук доставлен повторно (ожидаем duplicate, без дубля в БД)")
    post("order.created")
    asyncio.run(app.state.worker.tick())

    step("3. Приходит order.paid")
    post("order.paid")
    asyncio.run(app.state.worker.tick())

    step("4. Итоговое состояние базы")
    state = app.state.store.snapshot()
    dump({
        "contacts": state["contacts"],
        "orders": state["orders"],
        "events": [
            {"id": e["id"], "order_id": e["order_id"], "event": e["event"]}
            for e in state["events"]
        ],
        "jobs": [
            {"id": j["id"], "type": j["type"], "status": j["status"], "attempts": j["attempts"]}
            for j in state["jobs"]
        ],
        "messages": state["messages"],
    })

    step("5. Вызовы, ушедшие бы в Bitrix24")
    for call in app.state.bitrix.calls:
        print(f"{call['method']}  {json.dumps(call['params'], ensure_ascii=False)}")
