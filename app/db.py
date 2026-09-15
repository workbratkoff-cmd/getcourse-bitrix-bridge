"""База: схема и все SQL-запросы."""
from __future__ import annotations

import json
import random
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .normalize import OrderEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  id                INTEGER PRIMARY KEY,
  dedup_key         TEXT NOT NULL UNIQUE,
  email             TEXT,
  phone             TEXT,
  name              TEXT,
  bitrix_contact_id TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
  order_id       TEXT PRIMARY KEY,
  contact_id     INTEGER NOT NULL REFERENCES contacts(id),
  offer          TEXT,
  cost           REAL,
  bitrix_deal_id TEXT,
  stage          TEXT NOT NULL DEFAULT 'new',
  notified_at    TEXT,  -- когда ушло сообщение об оплате
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

-- UNIQUE(order_id, event) — на этом держится защита от дублей
CREATE TABLE IF NOT EXISTS events (
  id           INTEGER PRIMARY KEY,
  order_id     TEXT NOT NULL,
  event        TEXT NOT NULL,
  contact_id   INTEGER NOT NULL REFERENCES contacts(id),
  payload_json TEXT NOT NULL,
  received_at  TEXT NOT NULL,
  UNIQUE (order_id, event)
);

-- очередь доставки в Bitrix24
CREATE TABLE IF NOT EXISTS jobs (
  id           INTEGER PRIMARY KEY,
  event_id     INTEGER NOT NULL UNIQUE REFERENCES events(id),
  type         TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',
  attempts     INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 8,
  run_after    TEXT NOT NULL,
  last_error   TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs (status, run_after);

CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY,
  channel    TEXT NOT NULL,
  order_id   TEXT,
  phone      TEXT,
  text       TEXT NOT NULL,
  status     TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str):
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # FastAPI обслуживает запросы в разных потоках, поэтому соединение
        # общее и закрыто локом. isolation_level=None — транзакциями
        # управляем сами, явными BEGIN/COMMIT.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # контакты

    def upsert_contact(self, contact, dedup_key: str) -> sqlite3.Row:
        # COALESCE, чтобы пустые поля не затирали уже известные
        ts = _now()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO contacts (dedup_key, email, phone, name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (dedup_key) DO UPDATE SET
                  email      = COALESCE(excluded.email, contacts.email),
                  phone      = COALESCE(excluded.phone, contacts.phone),
                  name       = COALESCE(NULLIF(excluded.name, ''), contacts.name),
                  updated_at = excluded.updated_at
                """,
                (dedup_key, contact.email, contact.phone, contact.full_name, ts, ts),
            )
            return self.conn.execute(
                "SELECT * FROM contacts WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()

    def get_contact(self, contact_id: int) -> sqlite3.Row:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM contacts WHERE id = ?", (contact_id,)
            ).fetchone()

    def set_bitrix_contact_id(self, contact_id: int, bitrix_contact_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE contacts SET bitrix_contact_id = ?, updated_at = ? WHERE id = ?",
                (str(bitrix_contact_id), _now(), contact_id),
            )

    # заказы

    def upsert_order(self, order_id: str, contact_id: int, offer: str, cost) -> sqlite3.Row:
        ts = _now()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO orders (order_id, contact_id, offer, cost, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (order_id) DO UPDATE SET
                  offer      = COALESCE(NULLIF(excluded.offer, ''), orders.offer),
                  cost       = COALESCE(excluded.cost, orders.cost),
                  updated_at = excluded.updated_at
                """,
                (order_id, contact_id, offer, cost, ts, ts),
            )
            return self.conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()

    def get_order(self, order_id: str) -> sqlite3.Row:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()

    def set_bitrix_deal_id(self, order_id: str, deal_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE orders SET bitrix_deal_id = ?, updated_at = ? WHERE order_id = ?",
                (str(deal_id), _now(), order_id),
            )

    def set_order_stage(self, order_id: str, stage: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE orders SET stage = ?, updated_at = ? WHERE order_id = ?",
                (stage, _now(), order_id),
            )

    def mark_order_notified(self, order_id: str) -> None:
        ts = _now()
        with self._lock:
            self.conn.execute(
                "UPDATE orders SET notified_at = ?, updated_at = ? WHERE order_id = ?",
                (ts, ts, order_id),
            )

    # приём события

    def ingest_event(self, data: OrderEvent, raw_payload: Any, max_attempts: int) -> dict:
        # одной транзакцией: контакт, заказ, событие и задача в очередь
        with self._lock:
            existing = self.conn.execute(
                "SELECT * FROM events WHERE order_id = ? AND event = ?",
                (data.order_id, data.event),
            ).fetchone()
            if existing:
                return {"duplicate": True, "event": existing}

            ts = _now()
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                contact = self.upsert_contact(data.contact, data.dedup_key)
                self.upsert_order(data.order_id, contact["id"], data.offer, data.cost)

                cursor = self.conn.execute(
                    """
                    INSERT INTO events (order_id, event, contact_id, payload_json, received_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        data.order_id,
                        data.event,
                        contact["id"],
                        json.dumps(raw_payload, ensure_ascii=False),
                        ts,
                    ),
                )
                event_id = cursor.lastrowid

                self.conn.execute(
                    """
                    INSERT INTO jobs (event_id, type, status, max_attempts, run_after,
                                      created_at, updated_at)
                    VALUES (?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (event_id, data.event, max_attempts, ts, ts, ts),
                )
                self.conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                # два одновременных вебхука: проигравший упёрся в UNIQUE
                self.conn.execute("ROLLBACK")
                race = self.conn.execute(
                    "SELECT * FROM events WHERE order_id = ? AND event = ?",
                    (data.order_id, data.event),
                ).fetchone()
                if race:
                    return {"duplicate": True, "event": race}
                raise
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

            return {
                "duplicate": False,
                "event": self.conn.execute(
                    "SELECT * FROM events WHERE id = ?", (event_id,)
                ).fetchone(),
                "contact": contact,
            }

    def get_event(self, event_id: int) -> sqlite3.Row:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()

    # очередь

    def claim_due_jobs(self, limit: int) -> list[sqlite3.Row]:
        # берём задачи, которым пора, и помечаем running
        ts = _now()
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM jobs
                 WHERE status = 'pending' AND run_after <= ?
                 ORDER BY run_after, id
                 LIMIT ?
                """,
                (ts, limit),
            ).fetchall()

            claimed = []
            for row in rows:
                cursor = self.conn.execute(
                    "UPDATE jobs SET status = 'running', updated_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (ts, row["id"]),
                )
                if cursor.rowcount == 1:
                    claimed.append(row)
            return claimed

    def complete_job(self, job_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE jobs SET status = 'done', last_error = NULL, updated_at = ? WHERE id = ?",
                (_now(), job_id),
            )

    def fail_job(self, job_id: int, error: Exception, backoff_base_sec: float,
                 backoff_max_sec: float) -> dict:
        # считаем попытку и назначаем следующую, либо сдаёмся
        message = str(error)[:1000]
        ts = _now()
        with self._lock:
            job = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            attempts = job["attempts"] + 1

            if attempts >= job["max_attempts"]:
                self.conn.execute(
                    "UPDATE jobs SET status = 'dead', attempts = ?, last_error = ?, "
                    "updated_at = ? WHERE id = ?",
                    (attempts, message, ts, job_id),
                )
                return {"status": "dead", "attempts": attempts, "run_after": None}

            # пауза удваивается: 2с, 4с, 8с... плюс случайная добавка,
            # чтобы накопленные задачи не ушли в Bitrix одной пачкой
            delay = min(backoff_base_sec * (2 ** (attempts - 1)), backoff_max_sec)
            delay += random.uniform(0, min(1.0, delay * 0.2))
            run_after = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()

            self.conn.execute(
                """
                UPDATE jobs SET status = 'pending', attempts = ?, run_after = ?,
                                last_error = ?, updated_at = ?
                 WHERE id = ?
                """,
                (attempts, run_after, message, ts, job_id),
            )
            return {"status": "pending", "attempts": attempts, "run_after": run_after}

    def kill_job(self, job_id: int, error: Exception) -> dict:
        # ошибка данных или прав — повторять нечего
        with self._lock:
            self.conn.execute(
                "UPDATE jobs SET status = 'dead', attempts = attempts + 1, "
                "last_error = ?, updated_at = ? WHERE id = ?",
                (str(error)[:1000], _now(), job_id),
            )
            job = self.conn.execute(
                "SELECT attempts FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return {"status": "dead", "attempts": job["attempts"], "run_after": None}

    def requeue_stuck_jobs(self) -> int:
        # процесс упал посреди работы — задачи остались running
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE jobs SET status = 'pending', updated_at = ? WHERE status = 'running'",
                (_now(),),
            )
            return cursor.rowcount

    def queue_stats(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
            return {row["status"]: row["count"] for row in rows}

    # сообщения и снимок состояния

    def record_message(self, channel: str, order_id: str | None, phone: str | None,
                       text: str, status: str) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO messages (channel, order_id, phone, text, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (channel, order_id, phone, text, status, _now()),
            )

    def snapshot(self) -> dict[str, list[dict]]:
        # всё содержимое базы для ручной проверки через /admin/state
        def rows(sql: str) -> list[dict]:
            return [dict(row) for row in self.conn.execute(sql).fetchall()]

        with self._lock:
            return {
                "contacts": rows("SELECT * FROM contacts ORDER BY id"),
                "orders": rows("SELECT * FROM orders ORDER BY created_at"),
                "events": rows(
                    "SELECT id, order_id, event, contact_id, received_at FROM events ORDER BY id"
                ),
                "jobs": rows("SELECT * FROM jobs ORDER BY id"),
                "messages": rows("SELECT * FROM messages ORDER BY id"),
            }
