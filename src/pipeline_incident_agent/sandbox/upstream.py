"""The upstream system (the shop's app database export), simulated.

Two kinds of upstream problems exist in real life, and the difference matters for remediation:

* One-off delivery glitches (a missing, truncated or duplicated file). Asking upstream to
  redeliver fixes them: the export runs again and produces a correct file.
* Persistent changes (a renamed, retyped, added or dropped column). These are deliberate on the
  upstream side, so a redelivery reproduces the same file. The pipeline has to adapt, or a human
  has to negotiate with the owning team.

Persistent changes live in `<sandbox>/upstream/changes.json` and are applied to every export
from their effective day on. `redeliver()` re-runs the export for one table and day.
"""

from __future__ import annotations

import json
from datetime import date

from .config import HISTORY_START, SandboxPaths
from .generator import COLUMNS, customers_for_day, orders_for_day, write_csv

CHANGE_KINDS = ("rename_column", "add_column", "drop_column", "format_currency", "append_suffix")


def load_changes(paths: SandboxPaths) -> list[dict]:
    if not paths.upstream_changes.exists():
        return []
    return json.loads(paths.upstream_changes.read_text(encoding="utf-8"))


def add_change(paths: SandboxPaths, change: dict) -> None:
    """Record a persistent change: {"kind", "table", "effective": "YYYY-MM-DD", ...kind-specific}."""
    if change["kind"] not in CHANGE_KINDS:
        raise ValueError(f"unknown upstream change kind {change['kind']!r}")
    changes = load_changes(paths) + [change]
    paths.upstream_changes.parent.mkdir(parents=True, exist_ok=True)
    paths.upstream_changes.write_text(json.dumps(changes, indent=2), encoding="utf-8")


def apply_changes(table: str, day: date, rows: list[dict], columns: list[str], changes: list[dict]):
    rows = [dict(r) for r in rows]
    columns = list(columns)
    for ch in changes:
        if ch["table"] != table or day < date.fromisoformat(ch["effective"]):
            continue
        kind = ch["kind"]
        if kind == "rename_column":
            columns = [ch["to"] if c == ch["from"] else c for c in columns]
            for r in rows:
                r[ch["to"]] = r.pop(ch["from"])
        elif kind == "add_column":
            columns.append(ch["column"])
            for i, r in enumerate(rows):
                r[ch["column"]] = ch["values"][i % len(ch["values"])]
        elif kind == "drop_column":
            columns.remove(ch["column"])
            for r in rows:
                r.pop(ch["column"])
        elif kind == "format_currency":
            for r in rows:
                r[ch["column"]] = f"${r[ch['column']]}"
        elif kind == "append_suffix":
            for r in rows:
                r[ch["column"]] = f"{r[ch['column']]}{ch['suffix']}"
    return rows, columns


def export_rows(table: str, day: date) -> list[dict]:
    if table == "customers":
        return customers_for_day(day, HISTORY_START)
    return orders_for_day(day, HISTORY_START)[table]


def redeliver(paths: SandboxPaths, table: str, day: date) -> int:
    """Upstream re-runs its export for one table/day: a correct file, plus any persistent changes."""
    rows, columns = apply_changes(table, day, export_rows(table, day), COLUMNS[table], load_changes(paths))
    return write_csv(paths.landing_file(table, day), rows, columns)
