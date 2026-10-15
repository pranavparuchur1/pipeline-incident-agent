"""Load landing files into the warehouse's `raw` schema.

Event tables are loaded one daily partition at a time with delete-then-insert, so rerunning a
day is idempotent. Columns are matched BY NAME against the raw table's contract, so a renamed or
retyped upstream column fails loudly here instead of silently loading NULLs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import duckdb

from .config import EVENT_TABLES, SandboxPaths

RAW_CONTRACT: dict[str, dict[str, str]] = {
    "products": {
        "product_id": "INTEGER",
        "sku": "VARCHAR",
        "name": "VARCHAR",
        "category": "VARCHAR",
        "unit_price": "DECIMAL(10,2)",
        "active": "BOOLEAN",
    },
    "customers": {
        "customer_id": "INTEGER",
        "email": "VARCHAR",
        "first_name": "VARCHAR",
        "country": "VARCHAR",
        "signup_date": "DATE",
    },
    "orders": {
        "order_id": "BIGINT",
        "customer_id": "INTEGER",
        "order_ts": "TIMESTAMP",
        "status": "VARCHAR",
        "channel": "VARCHAR",
        "currency": "VARCHAR",
        "order_total": "DECIMAL(12,2)",
    },
    "order_items": {
        "order_item_id": "BIGINT",
        "order_id": "BIGINT",
        "product_id": "INTEGER",
        "quantity": "INTEGER",
        "unit_price": "DECIMAL(10,2)",
    },
    "payments": {
        "payment_id": "BIGINT",
        "order_id": "BIGINT",
        "method": "VARCHAR",
        "amount": "DECIMAL(12,2)",
        "paid_ts": "TIMESTAMP",
    },
}
LINEAGE_COLUMNS = {"_dt": "DATE", "_source_file": "VARCHAR", "_loaded_at": "TIMESTAMP", "_run_id": "VARCHAR"}


class IngestError(RuntimeError):
    pass


@dataclass
class LoadResult:
    table: str
    day: date | None
    rows: int
    source_file: str


def ensure_raw_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("create schema if not exists raw")
    for table, cols in RAW_CONTRACT.items():
        ddl = ", ".join(f"{c} {t}" for c, t in {**cols, **LINEAGE_COLUMNS}.items())
        con.execute(f"create table if not exists raw.{table} ({ddl})")


def _file_columns(path: str) -> list[str]:
    """Read the header with the csv module rather than DuckDB's sniffer: a malformed file body can
    make the sniffer guess the wrong delimiter and report a misleading schema mismatch."""
    import csv

    with open(path, encoding="utf-8", newline="") as f:
        return next(csv.reader(f), [])


def _varchar_columns(cols: list[str]) -> str:
    return "{" + ", ".join(f"'{c}': 'VARCHAR'" for c in cols) + "}"


PARSERS = {
    # strip_currency: "$59.90" -> 59.90 (upstream started exporting formatted amounts)
    "strip_currency": "replace(replace({col}, '$', ''), ',', '')",
}


def load_ingest_config(paths: SandboxPaths) -> dict:
    """Per-table ingest settings that remediation actions may change (see remediation.py).

    {"orders": {"rename": {"total_amount": "order_total"}, "parsers": {"order_total": "strip_currency"},
                "ignore_columns": ["coupon_code"], "dedupe_key": "order_id"}}
    """
    if not paths.ingest_config.exists():
        return {}
    return json.loads(paths.ingest_config.read_text(encoding="utf-8"))


def load_table(
    con: duckdb.DuckDBPyConnection, paths: SandboxPaths, table: str, day: date | None, run_id: str
) -> LoadResult:
    path = paths.landing_file(table, day)
    if not path.exists():
        raise IngestError(f"landing file missing: {path.relative_to(paths.root).as_posix()}")
    src = str(path)
    contract = RAW_CONTRACT[table]
    cfg = load_ingest_config(paths).get(table, {})
    rename: dict[str, str] = cfg.get("rename", {})
    ignore = set(cfg.get("ignore_columns", []))
    file_cols = _file_columns(src)
    # contract column -> the file column it is read from
    source_of = {rename.get(c, c): c for c in file_cols if c not in ignore}
    unexpected = [c for c in file_cols if c not in ignore and rename.get(c, c) not in contract]
    missing = [c for c in contract if c not in source_of]
    if unexpected or missing:
        raise IngestError(
            f"schema mismatch loading raw.{table} from {path.name}: "
            f"missing columns {missing or '[]'}, unexpected columns {unexpected or '[]'}"
        )
    exprs = []
    for col, typ in contract.items():
        ref = f'"{source_of[col]}"'
        if parser := cfg.get("parsers", {}).get(col):
            ref = PARSERS[parser].format(col=ref)
        exprs.append(f"cast({ref} as {typ}) as {col}")
    dedupe = cfg.get("dedupe_key")
    qualify = f' qualify row_number() over (partition by "{source_of[dedupe]}") = 1' if dedupe else ""
    con.execute("begin")
    try:
        if day is None:
            con.execute(f"delete from raw.{table}")
        else:
            con.execute(f"delete from raw.{table} where _dt = ?", [day])
        con.execute(
            f"insert into raw.{table} by name "
            f"select {', '.join(exprs)}, ?::date as _dt, ? as _source_file, now()::timestamp as _loaded_at, "
            f"? as _run_id from read_csv(?, header=true, hive_partitioning=false, delim=',', quote='\"', "
            f"auto_detect=false, columns={_varchar_columns(file_cols)}){qualify}",
            [day, path.relative_to(paths.root).as_posix(), run_id, src],
        )
        rows = con.execute(
            f"select count(*) from raw.{table} where _dt is not distinct from ?::date", [day]
        ).fetchone()[0]
        con.execute("commit")
    except duckdb.Error as e:
        con.execute("rollback")
        raise IngestError(f"failed loading raw.{table} from {path.name}: {e}") from e
    return LoadResult(table, day, rows, path.relative_to(paths.root).as_posix())


def load_partition(paths: SandboxPaths, day: date, run_id: str) -> list[LoadResult]:
    """Load every event table for one day (and the products dimension if raw.products is empty)."""
    with duckdb.connect(str(paths.warehouse)) as con:
        ensure_raw_tables(con)
        results = []
        if con.execute("select count(*) from raw.products").fetchone()[0] == 0:
            results.append(load_table(con, paths, "products", None, run_id))
        for table in EVENT_TABLES:
            results.append(load_table(con, paths, table, day, run_id))
        return results
