"""Tiny SQLite CRM for the database-lookup sample."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Customer:
    id: int
    phone: str
    name: str
    tier: str
    open_tickets: int
    notes: str


class CRM:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    tier TEXT NOT NULL DEFAULT 'standard',
                    open_tickets INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            # Seed a couple of demo rows if empty.
            count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
            if count == 0:
                conn.executemany(
                    "INSERT INTO customers (phone, name, tier, open_tickets, notes) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            "+15551230001",
                            "Alex Rivera",
                            "gold",
                            1,
                            "Waiting on refund for order #88421",
                        ),
                        (
                            "+15551230002",
                            "Sam Chen",
                            "standard",
                            0,
                            "Prefers morning callbacks",
                        ),
                    ],
                )

    def get_by_phone(self, phone: str) -> Optional[Customer]:
        normalized = _normalize(phone)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM customers WHERE phone = ? OR phone = ?",
                (phone, normalized),
            ).fetchone()
            if not row:
                # Suffix match for local testing with varying E.164 prefixes.
                row = conn.execute(
                    "SELECT * FROM customers WHERE phone LIKE ?",
                    (f"%{normalized[-10:]}",),
                ).fetchone()
            if not row:
                return None
            return Customer(
                id=row["id"],
                phone=row["phone"],
                name=row["name"],
                tier=row["tier"],
                open_tickets=row["open_tickets"],
                notes=row["notes"],
            )

    def create_lead(self, phone: str, source: str = "inbound_call") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO leads (phone, source) VALUES (?, ?)",
                (phone, source),
            )
            return int(cur.lastrowid)


def _normalize(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit() or c == "+")
    return digits
