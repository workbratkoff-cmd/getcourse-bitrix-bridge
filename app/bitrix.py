"""Клиент Bitrix24 REST поверх входящего вебхука портала."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import httpx

from .logger import get_logger

log = get_logger("bitrix")


class RetryableError(Exception):
    """Временная: сеть, 5xx, троттлинг. Повторяем."""
    retryable = True


class PermanentError(Exception):
    """Ошибка данных или прав. Повторять нечего."""
    retryable = False


# коды Bitrix, при которых повтор имеет смысл
RETRYABLE_BITRIX_ERRORS = {
    "QUERY_LIMIT_EXCEEDED",
    "OPERATION_TIME_LIMIT",
    "INTERNAL_SERVER_ERROR",
    "OVERLOAD_LIMIT",
}


class BitrixClient:
    """stub — пишем в лог, какой метод и с какими данными вызвали бы.
    webhook — реальные POST на {webhook_url}/{method}.json
    """

    def __init__(self, settings):
        self.settings = settings
        self.calls: list[dict] = []  # история вызовов: нужна тестам и /admin/state
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(self, method: str, params: dict):
        self.calls.append({
            "method": method,
            "params": params,
            "at": datetime.now(timezone.utc).isoformat(),
        })

        if self.settings.mode == "stub":
            log.info("bitrix.call.stub", extra={"method": method, "params": params})
            return self._stub_result(method)

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.timeout_sec)

        url = f"{self.settings.webhook_url}/{method}.json"
        try:
            response = await self._client.post(url, json=params)
        except httpx.HTTPError as exc:
            # сеть отвалилась или таймаут — точно повторяем
            raise RetryableError(f"Bitrix24 недоступен: {exc}") from exc

        text = response.text
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise RetryableError(
                f"Bitrix24 вернул не-JSON (HTTP {response.status_code}): {text[:200]}"
            ) from exc

        if response.status_code >= 500 or response.status_code == 429:
            raise RetryableError(f"Bitrix24 HTTP {response.status_code}: {text[:200]}")

        if isinstance(body, dict) and body.get("error"):
            code = body["error"]
            description = body.get("error_description", code)
            if code in RETRYABLE_BITRIX_ERRORS:
                raise RetryableError(f"Bitrix24 {code}: {description}")
            raise PermanentError(f"Bitrix24 {code}: {description}")

        if response.status_code >= 400:
            raise PermanentError(f"Bitrix24 HTTP {response.status_code}: {text[:200]}")

        log.debug("bitrix.call.ok", extra={"method": method})
        return body.get("result")

    async def find_contact(self, email: str | None, phone: str | None) -> str | None:
        # сначала по email, потом по телефону
        for comm_type, value in (("EMAIL", email), ("PHONE", phone)):
            if not value:
                continue
            result = await self.call("crm.duplicate.findbycomm", {
                "entity_type": "CONTACT",
                "type": comm_type,
                "values": [value],
            })
            found = (result or {}).get("CONTACT") or []
            if found:
                return str(found[0])
        return None

    async def create_contact(self, first_name: str, last_name: str,
                             email: str | None, phone: str | None) -> str:
        fields = {
            "NAME": first_name,
            "LAST_NAME": last_name,
            "OPENED": "Y",
            "TYPE_ID": "CLIENT",
        }
        if email:
            fields["EMAIL"] = [{"VALUE": email, "VALUE_TYPE": "WORK"}]
        if phone:
            fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "MOBILE"}]

        contact_id = await self.call("crm.contact.add", {
            "fields": fields,
            "params": {"REGISTER_SONET_EVENT": "N"},
        })
        return str(contact_id)

    async def create_deal(self, title: str, contact_id: str, opportunity,
                          order_id: str, stage_id: str, category_id: str) -> str:
        deal_id = await self.call("crm.deal.add", {
            "fields": {
                "TITLE": title,
                "CONTACT_ID": contact_id,
                "OPPORTUNITY": opportunity or 0,
                "CURRENCY_ID": "RUB",
                "CATEGORY_ID": category_id,
                "STAGE_ID": stage_id,
                # в бою номер заказа лучше класть в своё поле
                # UF_CRM_GETCOURSE_ORDER_ID, а не в комментарий
                "COMMENTS": f"GetCourse order_id: {order_id}",
                "SOURCE_ID": "WEB",
            },
            "params": {"REGISTER_SONET_EVENT": "N"},
        })
        return str(deal_id)

    async def update_deal_stage(self, deal_id: str, stage_id: str) -> None:
        await self.call("crm.deal.update", {"id": deal_id, "fields": {"STAGE_ID": stage_id}})

    def _stub_result(self, method: str):
        # правдоподобные id, одинаковые при одинаковых входных данных
        params = self.calls[-1]["params"] if self.calls else {}
        payload = json.dumps(params, ensure_ascii=False, sort_keys=True)
        seed = hashlib.sha1(f"{method}:{payload}".encode()).hexdigest()
        fake_id = str(100000 + int(seed[:6], 16) % 900000)

        if method == "crm.duplicate.findbycomm":
            # в заглушке контакта в портале нет — пойдём создавать
            return {"CONTACT": []}
        if method == "crm.deal.update":
            return True
        return fake_id
