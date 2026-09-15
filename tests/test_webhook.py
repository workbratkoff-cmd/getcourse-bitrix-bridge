from concurrent.futures import ThreadPoolExecutor

from .conftest import SECRET, order_payload, snapshot, webhook


def test_wrong_secret_returns_401(client):
    response = webhook(client, order_payload(), secret="wrong")
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_secret"}
    # Ничего не должно было записаться в базу.
    assert snapshot(client)["events"] == []


def test_secret_can_be_passed_as_header(client):
    response = client.post(
        "/webhooks/getcourse",
        json=order_payload(),
        headers={"X-Webhook-Secret": SECRET},
    )
    assert response.status_code == 202


def test_valid_event_returns_202_with_normalized_data(client):
    response = webhook(client, order_payload())
    assert response.status_code == 202

    body = response.json()
    assert body["status"] == "accepted"
    assert body["normalized"] == {
        "email": "anna.petrova@mail.ru",
        "phone": "+79161234567",
        "cost": 24900,
    }

    state = snapshot(client)
    assert len(state["contacts"]) == 1
    assert state["contacts"][0]["email"] == "anna.petrova@mail.ru"
    assert state["contacts"][0]["phone"] == "+79161234567"
    assert state["orders"][0]["cost"] == 24900
    assert len(state["jobs"]) == 1
    assert state["jobs"][0]["status"] == "pending"


def test_broken_json_and_invalid_payload_return_400(client):
    broken = webhook(client, raw=b"{not json")
    assert broken.status_code == 400
    assert broken.json()["error"] == "invalid_json"

    invalid = webhook(client, {"event": "order.refunded", "order_id": "", "user": {}})
    assert invalid.status_code == 400
    body = invalid.json()
    assert body["error"] == "invalid_payload"
    assert len(body["details"]) >= 2


def test_redelivery_does_not_create_duplicate(client):
    first = webhook(client, order_payload())
    second = webhook(client, order_payload())
    third = webhook(client, order_payload())

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert third.status_code == 200

    state = snapshot(client)
    assert len(state["events"]) == 1, "событие сохранено ровно один раз"
    assert len(state["jobs"]) == 1, "задача в очереди одна"
    assert len(state["contacts"]) == 1


def test_concurrent_deliveries_keep_single_row(client):
    """Пять одновременных доставок одного вебхука: ровно один 202."""
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(lambda _: webhook(client, order_payload()), range(5)))

    statuses = [r.status_code for r in responses]
    assert statuses.count(202) == 1
    assert statuses.count(200) == 4
    assert len(snapshot(client)["events"]) == 1


def test_created_and_paid_are_separate_events_of_one_order(client):
    webhook(client, order_payload())
    paid = webhook(client, order_payload(event="order.paid"))

    assert paid.status_code == 202
    state = snapshot(client)
    assert len(state["events"]) == 2
    assert len(state["orders"]) == 1, "заказ остаётся один"


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["bitrix_mode"] == "stub"


def test_admin_state_requires_secret(client):
    assert client.get("/admin/state").status_code == 401
    assert client.get(f"/admin/state?secret={SECRET}").status_code == 200
