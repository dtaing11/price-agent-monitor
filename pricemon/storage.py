"""SQLite persistence: watched products, price history, alert log."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

from .models import Alert, Extraction, Product, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT UNIQUE NOT NULL,
    url              TEXT NOT NULL,
    title            TEXT,
    image            TEXT,
    group_name       TEXT,
    selector         TEXT,
    learned_selector TEXT,
    target_price     REAL,
    currency         TEXT,
    active           INTEGER NOT NULL DEFAULT 1,
    notes            TEXT DEFAULT '',
    created_at       TEXT NOT NULL,
    last_checked     TEXT,
    last_price       REAL,
    last_in_stock    INTEGER,
    fail_count       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS observations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    ts         TEXT NOT NULL,
    price      REAL,
    currency   TEXT,
    in_stock   INTEGER,
    method     TEXT,
    title      TEXT,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_product_ts ON observations(product_id, ts);
CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    message    TEXT NOT NULL,
    price      REAL
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Checks run several sites at once, so the connection is shared between
        # worker threads. SQLite itself is fine with that; the Python driver
        # needs check_same_thread off, and every statement goes through _lock so
        # two workers cannot interleave a write.
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        with self._lock:
            have = {r["name"] for r in self.conn.execute("PRAGMA table_info(products)")}
            for column, ddl in (
                ("title", "TEXT"),
                ("image", "TEXT"),
                ("group_name", "TEXT"),
            ):
                if column not in have:
                    self.conn.execute(f"ALTER TABLE products ADD COLUMN {column} {ddl}")

    # -- products ---------------------------------------------------------
    def add_product(self, p: Product) -> Product:
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO products
                   (name, url, title, image, group_name, selector, learned_selector,
                    target_price, currency, active, notes, created_at, last_checked,
                    last_price, last_in_stock, fail_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    p.name,
                    p.url,
                    p.title,
                    p.image,
                    p.group,
                    p.selector,
                    p.learned_selector,
                    p.target_price,
                    p.currency,
                    int(p.active),
                    p.notes,
                    p.created_at,
                    p.last_checked,
                    p.last_price,
                    None if p.last_in_stock is None else int(p.last_in_stock),
                    p.fail_count,
                ),
            )
            self.conn.commit()
            p.id = cur.lastrowid
            return p

    def _row_to_product(self, r: sqlite3.Row) -> Product:
        with self._lock:
            return Product(
                id=r["id"],
                name=r["name"],
                url=r["url"],
                title=r["title"],
                image=r["image"],
                group=r["group_name"],
                selector=r["selector"],
                learned_selector=r["learned_selector"],
                target_price=r["target_price"],
                currency=r["currency"],
                active=bool(r["active"]),
                notes=r["notes"] or "",
                created_at=r["created_at"],
                last_checked=r["last_checked"],
                last_price=r["last_price"],
                last_in_stock=None
                if r["last_in_stock"] is None
                else bool(r["last_in_stock"]),
                fail_count=r["fail_count"],
            )

    def get_product(self, name: str) -> Product | None:
        with self._lock:
            r = self.conn.execute(
                "SELECT * FROM products WHERE name = ?", (name,)
            ).fetchone()
            return self._row_to_product(r) if r else None

    def list_products(self, active_only: bool = False) -> list[Product]:
        with self._lock:
            sql = (
                "SELECT * FROM products"
                + (" WHERE active = 1" if active_only else "")
                + " ORDER BY name"
            )
            return [self._row_to_product(r) for r in self.conn.execute(sql)]

    def update_product(self, p: Product) -> None:
        with self._lock:
            self.conn.execute(
                """UPDATE products SET url=?, title=?, image=?, group_name=?,
                   selector=?, learned_selector=?, target_price=?, currency=?,
                   active=?, notes=?, last_checked=?, last_price=?, last_in_stock=?,
                   fail_count=? WHERE id=?""",
                (
                    p.url,
                    p.title,
                    p.image,
                    p.group,
                    p.selector,
                    p.learned_selector,
                    p.target_price,
                    p.currency,
                    int(p.active),
                    p.notes,
                    p.last_checked,
                    p.last_price,
                    None if p.last_in_stock is None else int(p.last_in_stock),
                    p.fail_count,
                    p.id,
                ),
            )
            self.conn.commit()

    def remove_product(self, name: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM products WHERE name = ?", (name,))
            self.conn.commit()
            return cur.rowcount > 0

    def group_members(self, group: str) -> list[Product]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM products WHERE group_name = ? ORDER BY name", (group,)
            )
            return [self._row_to_product(r) for r in rows]

    def groups(self) -> list[str]:
        with self._lock:
            return [
                r["group_name"]
                for r in self.conn.execute(
                    """SELECT group_name FROM products
                       WHERE group_name IS NOT NULL AND group_name != ''
                       GROUP BY group_name ORDER BY group_name"""
                )
            ]

    # -- observations -----------------------------------------------------
    def record(
        self, product: Product, ex: Extraction, error: str | None = None
    ) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO observations
                   (product_id, ts, price, currency, in_stock, method, title, error)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    product.id,
                    utcnow(),
                    ex.price,
                    ex.currency,
                    None if ex.in_stock is None else int(ex.in_stock),
                    ex.method,
                    ex.title,
                    error,
                ),
            )
            self.conn.commit()

    def history(self, product: Product, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM observations WHERE product_id = ?
                   ORDER BY ts DESC LIMIT ?""",
                (product.id, limit),
            ).fetchall()
            return list(reversed(rows))

    def price_stats(self, product: Product):
        with self._lock:
            return self.conn.execute(
                """SELECT MIN(price) lo, MAX(price) hi, AVG(price) avg, COUNT(price) n
                   FROM observations WHERE product_id = ? AND price IS NOT NULL""",
                (product.id,),
            ).fetchone()

    def price_context(self, product: Product, price: float) -> dict:
        """How good is `price`, judged against everything seen so far?

        Returns the all-time low, the date it was seen, how many days of
        history exist, and how many of the recorded prices this one beats.
        """
        with self._lock:
            row = self.conn.execute(
                """SELECT MIN(price) lo, MAX(price) hi, COUNT(price) n,
                          MIN(ts) first_ts
                   FROM observations WHERE product_id = ? AND price IS NOT NULL""",
                (product.id,),
            ).fetchone()
            if not row or not row["n"]:
                return {"points": 0}

            low_row = self.conn.execute(
                """SELECT ts FROM observations
                   WHERE product_id = ? AND price IS NOT NULL
                   ORDER BY price ASC, ts DESC LIMIT 1""",
                (product.id,),
            ).fetchone()
            beaten = self.conn.execute(
                """SELECT COUNT(*) c FROM observations
                   WHERE product_id = ? AND price IS NOT NULL AND price > ?""",
                (product.id, price),
            ).fetchone()["c"]

            days = 0
            if row["first_ts"]:
                from datetime import datetime, timezone

                try:
                    first = datetime.fromisoformat(row["first_ts"])
                    if first.tzinfo is None:
                        first = first.replace(tzinfo=timezone.utc)
                    days = max(0, (datetime.now(timezone.utc) - first).days)
                except ValueError:
                    days = 0

            return {
                "points": row["n"],
                "low": row["lo"],
                "high": row["hi"],
                "low_ts": low_row["ts"] if low_row else None,
                "days": days,
                "beats": beaten,
            }

    # -- alerts -----------------------------------------------------------
    def record_alerts(self, product: Product, alerts: Iterable[Alert]) -> None:
        with self._lock:
            for a in alerts:
                self.conn.execute(
                    "INSERT INTO alerts (product_id, ts, kind, message, price) VALUES (?,?,?,?,?)",
                    (product.id, utcnow(), a.kind, a.message, a.price),
                )
            self.conn.commit()

    def recent_alerts(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    """SELECT a.*, p.name FROM alerts a JOIN products p ON p.id = a.product_id
                   ORDER BY a.ts DESC LIMIT ?""",
                    (limit,),
                )
            )

    def close(self) -> None:
        with self._lock:
            self.conn.close()
