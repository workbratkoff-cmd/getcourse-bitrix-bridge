from app.normalize import (
    normalize_cost,
    normalize_email,
    normalize_phone,
    parse_order_event,
    split_name,
)


def test_email_trims_and_lowercases():
    assert normalize_email(" Anna.Petrova@Mail.ru ") == "anna.petrova@mail.ru"
    assert normalize_email("ANNA@MAIL.RU") == "anna@mail.ru"
    assert normalize_email("не-email") is None
    assert normalize_email(None) is None


def test_phone_converts_to_e164():
    assert normalize_phone("8 (916) 123-45-67") == "+79161234567"
    assert normalize_phone("+7 916 123 45 67") == "+79161234567"
    assert normalize_phone("79161234567") == "+79161234567"
    assert normalize_phone("9161234567") == "+79161234567"
    assert normalize_phone("8-916-123-45-67") == "+79161234567"


def test_phone_rejects_garbage_and_keeps_foreign_codes():
    assert normalize_phone("123") is None
    assert normalize_phone("") is None
    assert normalize_phone("+380 67 123 45 67") == "+380671234567"


def test_cost_parsing():
    assert normalize_cost("24900") == 24900
    assert normalize_cost("24 900,50") == 24900.5
    assert normalize_cost(1990) == 1990
    assert normalize_cost("бесплатно") is None


def test_split_name():
    assert split_name("Анна Петрова") == ("Анна", "Петрова", "Анна Петрова")
    assert split_name("  Анна   ") == ("Анна", "", "Анна")
    assert split_name(None) == ("", "", "")


def test_parses_example_from_assignment():
    parsed = parse_order_event({
        "event": "order.paid",
        "order_id": "4817352",
        "cost": "24900",
        "offer": "Курс «Таргет с нуля»",
        "user": {
            "name": "Анна Петрова",
            "email": " Anna.Petrova@Mail.ru ",
            "phone": "8 (916) 123-45-67",
        },
    })

    assert parsed.ok
    assert parsed.data.order_id == "4817352"
    assert parsed.data.cost == 24900
    assert parsed.data.contact.email == "anna.petrova@mail.ru"
    assert parsed.data.contact.phone == "+79161234567"
    assert parsed.data.dedup_key == "anna.petrova@mail.ru"


def test_rejects_unknown_event_and_empty_order_id():
    parsed = parse_order_event({"event": "order.refunded", "user": {"email": "a@b.ru"}})
    assert not parsed.ok
    assert any("event" in e for e in parsed.errors)
    assert any("order_id" in e for e in parsed.errors)


def test_requires_at_least_one_contact():
    parsed = parse_order_event({"event": "order.created", "order_id": "1", "user": {}})
    assert not parsed.ok
    assert any("user.email" in e for e in parsed.errors)
