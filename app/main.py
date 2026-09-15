"""HTTP: приём вебхуков GetCourse и служебные ручки."""
from __future__ import annotations

import hmac
import json
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .bitrix import BitrixClient
from .config import Settings, load_settings
from .db import Store
from .logger import get_logger, setup_logging
from .normalize import parse_order_event
from .processor import EventProcessor
from .whatsapp import WhatsAppClient
from .worker import Worker

log = get_logger("http")

MAX_BODY_BYTES = 256 * 1024


class JsonResponse(JSONResponse):
    # без явного charset некоторые клиенты читают ответ как latin-1
    # и ломают кириллицу
    media_type = "application/json; charset=utf-8"


def secret_matches(provided: str | None, expected: str) -> bool:
    # сравниваем за постоянное время, чтобы секрет не подобрали по таймингу
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def create_app(settings: Settings | None = None) -> FastAPI:
    # функция, а не глобальный объект: тесты поднимают то же самое
    # на базе в памяти и с выключенным воркером
    settings = settings or load_settings()
    setup_logging(settings.log_level)

    store = Store(settings.db_path)
    bitrix = BitrixClient(settings.bitrix)
    whatsapp = WhatsAppClient(settings.whatsapp, store)
    processor = EventProcessor(settings, store, bitrix, whatsapp)
    worker = Worker(settings, store, processor)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.worker.enabled:
            worker.start()
        log.info("service.started", extra={
            "bitrix_mode": settings.bitrix.mode,
            "whatsapp_mode": settings.whatsapp.mode,
            "worker": settings.worker.enabled,
            "db": settings.db_path,
        })
        yield
        await worker.stop()
        await bitrix.aclose()
        store.close()
        log.info("service.stopped")

    app = FastAPI(
        title="GetCourse -> Bitrix24 bridge",
        version="1.0.0",
        lifespan=lifespan,
        default_response_class=JsonResponse,
    )

    # чтобы тесты и демо могли дотянуться до внутренностей
    app.state.settings = settings
    app.state.store = store
    app.state.bitrix = bitrix
    app.state.worker = worker

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "bitrix_mode": settings.bitrix.mode,
            "whatsapp_mode": settings.whatsapp.mode,
            "queue": store.queue_stats(),
        }

    @app.get("/admin/state")
    async def admin_state(secret: str | None = None):
        # весь дамп базы — чтобы глазами проверить, что записалось
        if not secret_matches(secret, settings.webhook_secret):
            return JsonResponse({"error": "invalid_secret"}, status_code=401)
        return {**store.snapshot(), "bitrix_calls": bitrix.calls}

    @app.post("/admin/drain")
    async def admin_drain(secret: str | None = None):
        # прогнать очередь руками, если воркер выключен
        if not secret_matches(secret, settings.webhook_secret):
            return JsonResponse({"error": "invalid_secret"}, status_code=401)
        result = await worker.tick()
        return {**result, "queue": store.queue_stats()}

    @app.post("/webhooks/getcourse")
    async def getcourse_webhook(request: Request, secret: str | None = None):
        request_id = str(uuid.uuid4())

        # GetCourse шлёт секрет в query, но заголовок тоже принимаем
        provided = secret or request.headers.get("x-webhook-secret")
        if not secret_matches(provided, settings.webhook_secret):
            log.warning("webhook.unauthorized", extra={
                "request_id": request_id,
                "remote": request.client.host if request.client else None,
            })
            return JsonResponse({"error": "invalid_secret"}, status_code=401)

        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return JsonResponse({"error": "payload_too_large"}, status_code=413)

        try:
            payload = json.loads(raw or b"null")
        except json.JSONDecodeError:
            return JsonResponse({"error": "invalid_json"}, status_code=400)

        parsed = parse_order_event(payload, settings.default_country_code)
        if not parsed.ok:
            log.warning("webhook.invalid_payload", extra={
                "request_id": request_id, "errors": parsed.errors,
            })
            # 400, а не 422: повтором это не чинится
            return JsonResponse(
                {"error": "invalid_payload", "details": parsed.errors}, status_code=400
            )

        data = parsed.data
        result = store.ingest_event(data, payload, settings.worker.max_attempts)

        if result["duplicate"]:
            log.info("webhook.duplicate", extra={
                "request_id": request_id, "order_id": data.order_id, "event": data.event,
            })
            return JsonResponse({
                "status": "duplicate",
                "order_id": data.order_id,
                "event": data.event,
                "event_id": result["event"]["id"],
            }, status_code=200)

        log.info("webhook.accepted", extra={
            "request_id": request_id,
            "order_id": data.order_id,
            "event": data.event,
            "event_id": result["event"]["id"],
            "email": data.contact.email,
            "phone": data.contact.phone,
        })

        # отвечаем сразу, не дожидаясь Bitrix24: вебхук GetCourse
        # не должен висеть на времени ответа чужого API
        return JsonResponse({
            "status": "accepted",
            "order_id": data.order_id,
            "event": data.event,
            "event_id": result["event"]["id"],
            "normalized": {
                "email": data.contact.email,
                "phone": data.contact.phone,
                "cost": data.cost,
            },
        }, status_code=202)

    return app
