import time

from app.bitrix import PermanentError, RetryableError

from .conftest import (
    bitrix_methods,
    drain,
    inject_bitrix_failures,
    order_payload,
    snapshot,
    webhook,
)


def test_order_created_finds_or_creates_contact_and_deal(client):
    webhook(client, order_payload())
    assert drain(client) == {"processed": 1, "failed": 0}

    # Поиск дубля идёт сначала по email, потом по телефону — в заглушке оба промахиваются.
    assert bitrix_methods(client) == [
        "crm.duplicate.findbycomm",
        "crm.duplicate.findbycomm",
        "crm.contact.add",
        "crm.deal.add",
    ]

    state = snapshot(client)
    assert state["contacts"][0]["bitrix_contact_id"], "id контакта сохранён локально"
    assert state["orders"][0]["bitrix_deal_id"], "id сделки сохранён локально"
    assert state["orders"][0]["stage"] == "new"
    assert state["jobs"][0]["status"] == "done"

    deal_call = next(c for c in client.app_state.bitrix.calls if c["method"] == "crm.deal.add")
    assert deal_call["params"]["fields"]["OPPORTUNITY"] == 24900
    assert "4817352" in deal_call["params"]["fields"]["COMMENTS"]


def test_order_paid_moves_deal_and_sends_whatsapp(client):
    webhook(client, order_payload())
    drain(client)
    webhook(client, order_payload(event="order.paid"))
    drain(client)

    update = next(c for c in client.app_state.bitrix.calls if c["method"] == "crm.deal.update")
    assert update["params"]["fields"]["STAGE_ID"] == "WON"

    state = snapshot(client)
    assert state["orders"][0]["stage"] == "paid"
    assert state["orders"][0]["notified_at"]

    assert len(state["messages"]) == 1
    message = state["messages"][0]
    assert message["channel"] == "whatsapp"
    assert message["phone"] == "+79161234567"
    assert message["status"] == "sent_stub"
    assert "Анна" in message["text"]
    assert "Таргет с нуля" in message["text"]

    assert bitrix_methods(client).count("crm.deal.add") == 1


def test_paid_arriving_before_created(client):
    """Порядок доставки вебхуков не гарантирован — оплата может прийти первой."""
    webhook(client, order_payload(event="order.paid"))
    drain(client)

    assert bitrix_methods(client) == [
        "crm.duplicate.findbycomm",
        "crm.duplicate.findbycomm",
        "crm.contact.add",
        "crm.deal.add",
        "crm.deal.update",
    ]

    state = snapshot(client)
    assert state["orders"][0]["stage"] == "paid"
    assert len(state["messages"]) == 1

    # Опоздавший order.created не должен создать вторую сделку.
    webhook(client, order_payload(event="order.created"))
    drain(client)
    assert bitrix_methods(client).count("crm.deal.add") == 1


def test_bitrix_unavailable_event_is_not_lost(client):
    restore = inject_bitrix_failures(client, 2, RetryableError("ECONNREFUSED"))
    webhook(client, order_payload())

    # Две неудачные попытки: задача остаётся в очереди, счётчик растёт.
    assert drain(client) == {"processed": 0, "failed": 1}
    assert snapshot(client)["jobs"][0]["status"] == "pending"
    assert snapshot(client)["jobs"][0]["attempts"] == 1

    time.sleep(0.02)
    assert drain(client) == {"processed": 0, "failed": 1}
    assert snapshot(client)["jobs"][0]["attempts"] == 2
    assert "ECONNREFUSED" in snapshot(client)["jobs"][0]["last_error"]

    # Bitrix поднялся — третья попытка проходит, дублей не появилось.
    restore()
    time.sleep(0.02)
    assert drain(client) == {"processed": 1, "failed": 0}

    state = snapshot(client)
    assert state["jobs"][0]["status"] == "done"
    assert state["jobs"][0]["last_error"] is None
    assert state["orders"][0]["bitrix_deal_id"]
    assert bitrix_methods(client).count("crm.deal.add") == 1


def test_exhausted_attempts_go_to_dead_letter(client):
    inject_bitrix_failures(client, 10**6, RetryableError("Bitrix24 лежит"))
    webhook(client, order_payload())

    # max_attempts = 3 в тестовой конфигурации.
    for _ in range(3):
        drain(client)
        time.sleep(0.02)

    job = snapshot(client)["jobs"][0]
    assert job["status"] == "dead"
    assert job["attempts"] == 3

    # Событие сохранено — данные не потеряны, задачу можно перезапустить.
    assert len(snapshot(client)["events"]) == 1


def test_permanent_error_is_not_retried(client):
    inject_bitrix_failures(client, 10**6, PermanentError("Bitrix24 INVALID_CREDENTIALS"))
    webhook(client, order_payload())
    drain(client)

    job = snapshot(client)["jobs"][0]
    assert job["status"] == "dead"
    assert job["attempts"] == 1


def test_reprocessing_done_job_changes_nothing(client):
    """Имитация падения процесса сразу после обработки: задача снова pending."""
    webhook(client, order_payload(event="order.paid"))
    drain(client)
    calls_after_first = len(client.app_state.bitrix.calls)

    client.app_state.store.conn.execute("UPDATE jobs SET status = 'pending'")
    drain(client)

    state = snapshot(client)
    assert len(state["messages"]) == 1, "второе сообщение клиенту не ушло"
    assert state["orders"][0]["stage"] == "paid"
    assert len(client.app_state.bitrix.calls) == calls_after_first


def test_two_clients_stay_separate(client):
    webhook(client, order_payload())
    webhook(client, order_payload(
        order_id="4817353",
        user={"name": "Иван Смирнов", "email": "ivan@mail.ru", "phone": "+7 999 000 11 22"},
    ))
    drain(client)

    state = snapshot(client)
    assert len(state["contacts"]) == 2
    assert len(state["orders"]) == 2
    assert state["orders"][0]["bitrix_deal_id"] != state["orders"][1]["bitrix_deal_id"]
