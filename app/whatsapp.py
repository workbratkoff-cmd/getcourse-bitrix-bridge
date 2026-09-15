"""Отправка в WhatsApp. Сейчас заглушка: текст идёт в лог и в таблицу messages.

Реальный шлюз (Wazzup, WABA, свой на VPS) встаёт сюда же — send() не меняется.
"""
from __future__ import annotations

from .logger import get_logger

log = get_logger("whatsapp")


class WhatsAppClient:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store

    async def send(self, phone: str | None, text: str, order_id: str) -> str:
        if not phone:
            log.warning("whatsapp.skipped", extra={"order_id": order_id, "reason": "no_phone"})
            self.store.record_message("whatsapp", order_id, None, text, "skipped")
            return "skipped"

        if self.settings.mode == "stub":
            log.info("whatsapp.send.stub", extra={"to": phone, "order_id": order_id, "text": text})
            self.store.record_message("whatsapp", order_id, phone, text, "sent_stub")
            return "sent_stub"

        raise NotImplementedError(f"WHATSAPP__MODE={self.settings.mode} не реализован")


def paid_message(name: str | None, offer: str, cost: float | None) -> str:
    # текст, который уходит клиенту после оплаты
    first_name = (name or "").split(" ")[0] or "Здравствуйте"
    amount = ""
    if cost is not None:
        # неразрывный пробел между разрядами: 24 900 ₽
        amount = f" на сумму {cost:,.0f}".replace(",", " ") + " ₽"
    return (
        f"{first_name}, спасибо за оплату! Заказ {offer}{amount} подтверждён. "
        "Доступ к материалам придёт на вашу почту в течение 5 минут."
    )
