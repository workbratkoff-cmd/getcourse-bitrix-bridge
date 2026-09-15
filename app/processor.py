"""Что делаем в Bitrix24 и WhatsApp по каждому событию.

Каждый шаг сначала смотрит, не сделан ли он уже, и только потом идёт наружу —
поэтому повтор задачи не создаёт вторую сделку и не шлёт второе сообщение.
"""
from __future__ import annotations

import sqlite3

from .logger import get_logger
from .normalize import split_name
from .whatsapp import paid_message

log = get_logger("processor")


class EventProcessor:
    def __init__(self, settings, store, bitrix, whatsapp):
        self.settings = settings
        self.store = store
        self.bitrix = bitrix
        self.whatsapp = whatsapp

    async def process(self, job: sqlite3.Row) -> dict:
        event = self.store.get_event(job["event_id"])
        ctx = {"job_id": job["id"], "order_id": event["order_id"], "event": event["event"]}

        contact_id = await self._ensure_contact(event["contact_id"], ctx)
        deal_id = await self._ensure_deal(event["order_id"], contact_id, ctx)

        if event["event"] == "order.paid":
            await self._mark_paid(event["order_id"], deal_id, ctx)
            await self._notify_paid(event["order_id"], ctx)

        log.info("event.processed", extra={
            **ctx, "bitrix_contact_id": contact_id, "bitrix_deal_id": deal_id,
        })
        return {"contact_id": contact_id, "deal_id": deal_id}

    async def _ensure_contact(self, local_contact_id: int, ctx: dict) -> str:
        # ищем контакт в портале, если нет — создаём; id запоминаем у себя
        contact = self.store.get_contact(local_contact_id)
        if contact["bitrix_contact_id"]:
            return contact["bitrix_contact_id"]

        found = await self.bitrix.find_contact(contact["email"], contact["phone"])
        if found:
            bitrix_id = found
        else:
            first_name, last_name, _ = split_name(contact["name"])
            bitrix_id = await self.bitrix.create_contact(
                first_name, last_name, contact["email"], contact["phone"]
            )

        self.store.set_bitrix_contact_id(contact["id"], bitrix_id)
        log.info(
            "bitrix.contact.matched" if found else "bitrix.contact.created",
            extra={**ctx, "bitrix_contact_id": bitrix_id},
        )
        return bitrix_id

    async def _ensure_deal(self, order_id: str, bitrix_contact_id: str, ctx: dict) -> str:
        # вызывается и на order.paid: если оплата пришла первой,
        # сделка создаётся здесь же и сразу переводится в «Оплачено»
        order = self.store.get_order(order_id)
        if order["bitrix_deal_id"]:
            return order["bitrix_deal_id"]

        deal_id = await self.bitrix.create_deal(
            title=f"{order['offer'] or 'Заказ'} — {order_id}",
            contact_id=bitrix_contact_id,
            opportunity=order["cost"],
            order_id=order_id,
            stage_id=self.settings.bitrix.stage_new,
            category_id=self.settings.bitrix.category_id,
        )
        self.store.set_bitrix_deal_id(order_id, deal_id)
        log.info("bitrix.deal.created", extra={**ctx, "bitrix_deal_id": deal_id})
        return deal_id

    async def _mark_paid(self, order_id: str, deal_id: str, ctx: dict) -> None:
        order = self.store.get_order(order_id)
        if order["stage"] == "paid":
            log.debug("bitrix.deal.already_paid", extra={**ctx, "bitrix_deal_id": deal_id})
            return

        await self.bitrix.update_deal_stage(deal_id, self.settings.bitrix.stage_paid)
        self.store.set_order_stage(order_id, "paid")
        log.info("bitrix.deal.stage_updated", extra={
            **ctx, "bitrix_deal_id": deal_id, "stage": self.settings.bitrix.stage_paid,
        })

    async def _notify_paid(self, order_id: str, ctx: dict) -> None:
        order = self.store.get_order(order_id)
        if order["notified_at"]:
            log.debug("whatsapp.already_sent", extra=ctx)
            return

        contact = self.store.get_contact(order["contact_id"])
        await self.whatsapp.send(
            phone=contact["phone"],
            text=paid_message(contact["name"], order["offer"], order["cost"]),
            order_id=order_id,
        )
        self.store.mark_order_notified(order_id)
