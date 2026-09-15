"""Приводим данные из GetCourse к единому виду и проверяем их."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
SUPPORTED_EVENTS = ("order.created", "order.paid")

# \u00a0 — неразрывный пробел, часто прилетает из копипасты
_SPACES = re.compile(r"[\s\u00a0]")


def normalize_email(raw) -> str | None:
    # " Anna.Petrova@Mail.ru " -> "anna.petrova@mail.ru"
    if not isinstance(raw, str):
        return None
    value = _SPACES.sub("", raw).lower()
    return value if EMAIL_RE.match(value) else None


def normalize_phone(raw, default_country_code: str = "7") -> str | None:
    # "8 (916) 123-45-67" -> "+79161234567"
    if not isinstance(raw, str):
        return None

    trimmed = raw.strip()
    has_plus = trimmed.startswith("+")
    digits = re.sub(r"\D", "", trimmed)
    if not digits:
        return None

    if has_plus:
        e164 = digits
    elif default_country_code == "7" and len(digits) == 11 and digits[0] in ("8", "7"):
        # привычная российская восьмёрка
        e164 = "7" + digits[1:]
    elif default_country_code == "7" and len(digits) == 10:
        e164 = "7" + digits
    elif digits.startswith(default_country_code):
        e164 = digits
    else:
        e164 = default_country_code + digits

    # в E.164 от 8 до 15 цифр, остальное — мусор
    if not 8 <= len(e164) <= 15:
        return None
    return "+" + e164


def normalize_cost(raw) -> float | None:
    # "24 900,50" -> 24900.5
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if not isinstance(raw, str):
        return None
    try:
        return float(_SPACES.sub("", raw).replace(",", "."))
    except ValueError:
        return None


def split_name(raw) -> tuple[str, str, str]:
    # "Анна Петрова" -> ("Анна", "Петрова", "Анна Петрова")
    parts = str(raw or "").split()
    if not parts:
        return "", "", ""
    return parts[0], " ".join(parts[1:]), " ".join(parts)


@dataclass(frozen=True)
class Contact:
    email: str | None
    phone: str | None
    first_name: str
    last_name: str
    full_name: str


@dataclass(frozen=True)
class OrderEvent:
    event: str
    order_id: str
    offer: str
    cost: float | None
    dedup_key: str
    contact: Contact


@dataclass(frozen=True)
class ParseResult:
    ok: bool
    data: OrderEvent | None = None
    errors: list[str] = field(default_factory=list)


def parse_order_event(payload, default_country_code: str = "7") -> ParseResult:
    # разбираем тело вебхука: либо данные, либо список ошибок
    if not isinstance(payload, dict):
        return ParseResult(ok=False, errors=["payload должен быть JSON-объектом"])

    errors: list[str] = []

    event = payload.get("event")
    event = event.strip() if isinstance(event, str) else ""
    if event not in SUPPORTED_EVENTS:
        errors.append(f"event должен быть одним из: {', '.join(SUPPORTED_EVENTS)}")

    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        errors.append("order_id обязателен")

    user = payload.get("user")
    user = user if isinstance(user, dict) else {}
    email = normalize_email(user.get("email"))
    phone = normalize_phone(user.get("phone"), default_country_code)

    # заказ без контакта не на что повесить в CRM
    if not email and not phone:
        errors.append("нужен хотя бы один валидный контакт: user.email или user.phone")
    if user.get("email") is not None and not email:
        errors.append("user.email не похож на email")
    if user.get("phone") is not None and not phone:
        errors.append("user.phone не похож на телефон")

    cost = normalize_cost(payload.get("cost"))
    if payload.get("cost") is not None and cost is None:
        errors.append("cost не число")

    if errors:
        return ParseResult(ok=False, errors=errors)

    first_name, last_name, full_name = split_name(user.get("name"))
    return ParseResult(
        ok=True,
        data=OrderEvent(
            event=event,
            order_id=order_id,
            offer=str(payload.get("offer") or "").strip(),
            cost=cost,
            # по чему склеиваем контакты: email надёжнее телефона
            dedup_key=email or phone,
            contact=Contact(
                email=email,
                phone=phone,
                first_name=first_name,
                last_name=last_name,
                full_name=full_name,
            ),
        ),
    )
