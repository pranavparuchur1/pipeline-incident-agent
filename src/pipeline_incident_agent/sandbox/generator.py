"""Deterministic synthetic source data for the fake shop.

The "upstream" systems (the shop's app database) drop one CSV per table per day into the
landing zone, exactly like an export job would. Given the same seed and day, the files are
byte-identical, so every sandbox run starts from the same world.

Landing-file contract (the columns ingest expects):
  products     product_id, sku, name, category, unit_price, active
  customers    customer_id, email, first_name, country, signup_date
  orders       order_id, customer_id, order_ts, status, channel, currency, order_total
  order_items  order_item_id, order_id, product_id, quantity, unit_price
  payments     payment_id, order_id, method, amount, paid_ts
"""

from __future__ import annotations

import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import SEED, SandboxPaths

N_PRODUCTS = 120
N_INITIAL_CUSTOMERS = 2000
BASE_ORDERS_PER_DAY = 400

CATEGORIES = ["kitchen", "garden", "bath", "lighting", "textiles", "stationery"]
COUNTRIES = ["US", "US", "US", "CA", "GB", "DE", "AU"]
FIRST_NAMES = ["Ava", "Liam", "Mia", "Noah", "Zoe", "Ezra", "Ivy", "Omar", "Leah", "Ravi", "Nina", "Theo"]
CHANNELS = ["web", "web", "web", "app", "app", "marketplace"]
STATUSES = ["placed"] * 6 + ["shipped"] * 10 + ["cancelled"] + ["returned"]
METHODS = ["card", "card", "card", "paypal", "gift_card"]

COLUMNS = {
    "products": ["product_id", "sku", "name", "category", "unit_price", "active"],
    "customers": ["customer_id", "email", "first_name", "country", "signup_date"],
    "orders": ["order_id", "customer_id", "order_ts", "status", "channel", "currency", "order_total"],
    "order_items": ["order_item_id", "order_id", "product_id", "quantity", "unit_price"],
    "payments": ["payment_id", "order_id", "method", "amount", "paid_ts"],
}


def _rng(day: date | None, salt: str) -> random.Random:
    return random.Random(f"{SEED}:{salt}:{day.isoformat() if day else 'static'}")


def products() -> list[dict]:
    rng = _rng(None, "products")
    rows = []
    for pid in range(1, N_PRODUCTS + 1):
        cat = CATEGORIES[(pid - 1) % len(CATEGORIES)]
        rows.append(
            {
                "product_id": pid,
                "sku": f"LK-{cat[:3].upper()}-{pid:04d}",
                "name": f"{cat.title()} item {pid}",
                "category": cat,
                "unit_price": f"{rng.choice([4, 9, 14, 19, 24, 29, 39, 49, 79]) + 0.99:.2f}",
                "active": "true" if pid % 17 else "false",
            }
        )
    return rows


NEW_CUSTOMERS_PER_DAY = 30


def _customers_before(day: date, start: date) -> int:
    """Customer ids are dense: the initial base arrives on the start day, then a fixed number per day."""
    if day == start:
        return 0
    return N_INITIAL_CUSTOMERS + NEW_CUSTOMERS_PER_DAY * ((day - start).days - 1)


def _new_customers(day: date, start: date) -> int:
    return N_INITIAL_CUSTOMERS if day == start else NEW_CUSTOMERS_PER_DAY


def customers_for_day(day: date, start: date) -> list[dict]:
    rng = _rng(day, "customers")
    first = _customers_before(day, start) + 1
    count = _new_customers(day, start)
    rows = []
    for cid in range(first, first + count):
        name = rng.choice(FIRST_NAMES)
        rows.append(
            {
                "customer_id": cid,
                "email": f"{name.lower()}.{cid}@example.com",
                "first_name": name,
                "country": rng.choice(COUNTRIES),
                "signup_date": (day if day != start else day - timedelta(days=rng.randint(1, 700))).isoformat(),
            }
        )
    return rows


def orders_per_day(day: date, start: date) -> int:
    rng = _rng(day, "volume")
    weekend = 1.2 if day.weekday() >= 5 else 1.0
    growth = 1 + 0.004 * (day - start).days
    return int(BASE_ORDERS_PER_DAY * weekend * growth * rng.uniform(0.93, 1.07))


def _order_id_offset(day: date, start: date) -> int:
    return sum(orders_per_day(start + timedelta(days=i), start) for i in range((day - start).days))


def orders_for_day(day: date, start: date) -> dict[str, list[dict]]:
    """Return orders, order_items and payments for one day (ids are globally unique and dense)."""
    rng = _rng(day, "orders")
    prices = {p["product_id"]: float(p["unit_price"]) for p in products() if p["active"] == "true"}
    product_ids = sorted(prices)
    n_customers = _customers_before(day, start) + _new_customers(day, start)
    first_order = _order_id_offset(day, start) + 1
    orders, items, payments = [], [], []
    item_id = first_order * 10  # 10 id slots per order keeps item ids unique without global state
    for oid in range(first_order, first_order + orders_per_day(day, start)):
        ts = datetime(day.year, day.month, day.day) + timedelta(seconds=rng.randint(0, 86_399))
        status = rng.choice(STATUSES)
        total = 0.0
        for _ in range(rng.choice([1, 1, 1, 2, 2, 3, 4])):
            pid = rng.choice(product_ids)
            qty = rng.choice([1, 1, 1, 2, 3])
            items.append(
                {
                    "order_item_id": item_id,
                    "order_id": oid,
                    "product_id": pid,
                    "quantity": qty,
                    "unit_price": f"{prices[pid]:.2f}",
                }
            )
            item_id += 1
            total += qty * prices[pid]
        orders.append(
            {
                "order_id": oid,
                "customer_id": rng.randint(1, n_customers),
                "order_ts": ts.isoformat(sep=" "),
                "status": status,
                "channel": rng.choice(CHANNELS),
                "currency": "USD",
                "order_total": f"{total:.2f}",
            }
        )
        if status != "cancelled":
            payments.append(
                {
                    "payment_id": oid,
                    "order_id": oid,
                    "method": rng.choice(METHODS),
                    "amount": f"{total:.2f}",
                    "paid_ts": (ts + timedelta(seconds=rng.randint(5, 600))).isoformat(sep=" "),
                }
            )
        item_id = (oid + 1) * 10
    return {"orders": orders, "order_items": items, "payments": payments}


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def generate_day(paths: SandboxPaths, day: date, start: date) -> dict[str, int]:
    """Write every event table's landing file for one day. Returns row counts."""
    tables = {"customers": customers_for_day(day, start), **orders_for_day(day, start)}
    return {t: write_csv(paths.landing_file(t, day), rows, COLUMNS[t]) for t, rows in tables.items()}


def generate_products(paths: SandboxPaths) -> int:
    return write_csv(paths.landing_file("products", None), products(), COLUMNS["products"])
