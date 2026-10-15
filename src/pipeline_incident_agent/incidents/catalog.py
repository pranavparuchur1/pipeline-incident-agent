"""The incident catalog: every way we break the sandbox, with its ground truth.

Each incident is injected into the day after the clean baseline (INCIDENT_DAY), whose landing
files have been delivered but not yet processed. For every incident we record:

  root_cause      what actually went wrong (graded against the agent's diagnosis)
  symptom         where the run fails and which errors/checks show it (proved by the gate)
  outcome         "recover" (a fix exists) or "escalate" (no safe automated fix exists)
  true_fixes      alternative action sequences that genuinely restore the known-good state
  decoy_fixes     plausible-looking actions that must NOT restore it (wrong fixes)

The gate (harness.py) proves all of this mechanically.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..sandbox.config import SandboxPaths
from ..sandbox.generator import write_csv
from ..sandbox.upstream import add_change, export_rows, redeliver

Fix = list[tuple[str, dict]]  # a sequence of (action name, params)


@dataclass
class Incident:
    id: str
    family: str  # schema_change | missing_partition | duplicates | failed_job | row_count_drop
    title: str
    root_cause: dict  # {"family", "cause", plus table/column/model as relevant}
    expected_failed_step: str  # ingest | transform | quality_checks
    expected_evidence: list[str]  # substrings that must appear in the run error or failed check ids
    inject: Callable[[SandboxPaths, date], None]
    true_fixes: list[Fix]
    decoy_fixes: list[Fix] = field(default_factory=list)
    outcome: str = "recover"  # recover | escalate


# ---------------------------------------------------------------- landing-file helpers


def _read(paths: SandboxPaths, table: str, day: date) -> tuple[list[dict], list[str]]:
    with paths.landing_file(table, day).open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def _rewrite(paths: SandboxPaths, table: str, day: date, fn: Callable[[list[dict]], list[dict]]) -> None:
    rows, cols = _read(paths, table, day)
    write_csv(paths.landing_file(table, day), fn(rows), cols)


def _keep_orders(paths: SandboxPaths, day: date, keep: Callable[[dict], bool]) -> None:
    """A partial export: drop the same orders consistently from orders, order_items and payments."""
    rows, _ = _read(paths, "orders", day)
    kept = {r["order_id"] for r in rows if keep(r)}
    for t in ("orders", "order_items", "payments"):
        _rewrite(paths, t, day, lambda rs: [r for r in rs if r["order_id"] in kept])


def _upstream(change: dict) -> Callable[[SandboxPaths, date], None]:
    """A persistent upstream change, effective from the incident day, followed by that day's export."""

    def inject(paths: SandboxPaths, day: date) -> None:
        add_change(paths, {**change, "effective": day.isoformat()})
        redeliver(paths, change["table"], day)

    return inject


def _edit_model(rel_path: str, old: str, new: str) -> Callable[[SandboxPaths, date], None]:
    """A bad deploy: someone changed a dbt model in production."""

    def inject(paths: SandboxPaths, day: date) -> None:
        f = paths.dbt_project / rel_path
        text = f.read_text(encoding="utf-8")
        if old not in text:
            raise RuntimeError(f"cannot inject: {old!r} not found in {rel_path}")
        f.write_text(text.replace(old, new, 1), encoding="utf-8")

    return inject


def _delete(table: str) -> Callable[[SandboxPaths, date], None]:
    return lambda paths, day: paths.landing_file(table, day).unlink()


def _empty(table: str) -> Callable[[SandboxPaths, date], None]:
    return lambda paths, day: _rewrite(paths, table, day, lambda rows: [])


def _double(table: str, every: int = 1) -> Callable[[SandboxPaths, date], None]:
    """Upstream export retried and appended: every `every`-th row appears twice."""
    return lambda paths, day: _rewrite(paths, table, day, lambda rs: rs + rs[::every])


def _append_stale_orders(paths: SandboxPaths, day: date) -> None:
    """Yesterday's orders were re-sent inside today's file (ids collide with yesterday's partition)."""
    stale = export_rows("orders", day - timedelta(days=1))
    _rewrite(paths, "orders", day, lambda rs: rs + [{k: str(v) for k, v in r.items()} for r in stale])


def _truncate(table: str, fraction: float) -> Callable[[SandboxPaths, date], None]:
    """The export was cut off mid-write: the file ends part-way through a row."""

    def inject(paths: SandboxPaths, day: date) -> None:
        f = paths.landing_file(table, day)
        data = f.read_bytes()
        cut = int(len(data) * fraction)
        while data[cut - 1 : cut] in (b"\n", b","):  # make sure we end mid-row, mid-value
            cut += 1
        f.write_bytes(data[:cut])

    return inject


def _redeliver(*tables: str) -> Fix:
    return [("request_upstream_redelivery", {"tables": list(tables), "day": "{day}"})]


RERUN: Fix = [("rerun_only", {})]


# ---------------------------------------------------------------- the catalog

INCIDENTS: list[Incident] = [
    # -- schema changes (persistent upstream changes: redelivery reproduces them)
    Incident(
        "S1", "schema_change", "orders.order_total renamed to total_amount upstream",
        {"family": "schema_change", "cause": "column_renamed", "table": "orders", "column": "order_total"},
        "ingest", ["missing columns ['order_total']", "unexpected columns ['total_amount']"],
        _upstream({"kind": "rename_column", "table": "orders", "from": "order_total", "to": "total_amount"}),
        true_fixes=[[("apply_column_mapping",
                      {"table": "orders", "source_column": "total_amount", "target_column": "order_total"})]],
        decoy_fixes=[_redeliver("orders"), RERUN],
    ),
    Incident(
        "S2", "schema_change", "customers.country renamed to country_code upstream",
        {"family": "schema_change", "cause": "column_renamed", "table": "customers", "column": "country"},
        "ingest", ["missing columns ['country']", "unexpected columns ['country_code']"],
        _upstream({"kind": "rename_column", "table": "customers", "from": "country", "to": "country_code"}),
        true_fixes=[[("apply_column_mapping",
                      {"table": "customers", "source_column": "country_code", "target_column": "country"})]],
        decoy_fixes=[_redeliver("customers")],
    ),
    Incident(
        "S3", "schema_change", "orders.order_total now exported as a currency string ('$59.90')",
        {"family": "schema_change", "cause": "column_type_changed", "table": "orders", "column": "order_total"},
        "ingest", ["failed loading raw.orders", "$"],
        _upstream({"kind": "format_currency", "table": "orders", "column": "order_total"}),
        true_fixes=[[("set_column_parser", {"table": "orders", "column": "order_total", "parser": "strip_currency"})]],
        decoy_fixes=[_redeliver("orders"), RERUN],
    ),
    Incident(
        "S4", "schema_change", "orders gained a new upstream column coupon_code",
        {"family": "schema_change", "cause": "column_added", "table": "orders", "column": "coupon_code"},
        "ingest", ["unexpected columns ['coupon_code']"],
        _upstream({"kind": "add_column", "table": "orders", "column": "coupon_code",
                   "values": ["", "", "", "FALL10", "", "WELCOME5"]}),
        true_fixes=[[("ignore_extra_columns", {"table": "orders", "columns": ["coupon_code"]})]],
        decoy_fixes=[_redeliver("orders")],
    ),
    Incident(
        "S5", "schema_change", "payments.method dropped upstream (no source for a required column)",
        {"family": "schema_change", "cause": "column_dropped", "table": "payments", "column": "method"},
        "ingest", ["missing columns ['method']"],
        _upstream({"kind": "drop_column", "table": "payments", "column": "method"}),
        true_fixes=[[("escalate_to_owner",
                      {"owner": "upstream_app_team", "summary": "payments export no longer includes method"})]],
        decoy_fixes=[_redeliver("payments"), RERUN],
        outcome="escalate",
    ),
    # -- missing partitions (one-off delivery glitches: redelivery fixes them)
    Incident(
        "M1", "missing_partition", "orders file for the day never arrived",
        {"family": "missing_partition", "cause": "file_missing", "table": "orders"},
        "ingest", ["landing file missing", "orders"],
        _delete("orders"), true_fixes=[_redeliver("orders")], decoy_fixes=[RERUN],
    ),
    Incident(
        "M2", "missing_partition", "payments file arrived empty (header only)",
        {"family": "missing_partition", "cause": "file_empty", "table": "payments"},
        "quality_checks", ["partition_present:raw.payments", "assert_payments_match_orders"],
        _empty("payments"), true_fixes=[_redeliver("payments")], decoy_fixes=[RERUN],
    ),
    Incident(
        "M3", "missing_partition", "order_items file for the day never arrived",
        {"family": "missing_partition", "cause": "file_missing", "table": "order_items"},
        "ingest", ["landing file missing", "order_items"],
        _delete("order_items"), true_fixes=[_redeliver("order_items")], decoy_fixes=[RERUN],
    ),
    # -- duplicates
    Incident(
        "D1", "duplicates", "orders export retried and appended: every order appears twice",
        {"family": "duplicates", "cause": "duplicate_rows_in_file", "table": "orders"},
        "quality_checks", ["unique_stg_orders_order_id"],
        _double("orders"),
        true_fixes=[_redeliver("orders"), [("deduplicate_on_ingest", {"table": "orders", "key": "order_id"})]],
        decoy_fixes=[RERUN],
    ),
    Incident(
        "D2", "duplicates", "about 5% of order_items rows duplicated in the export",
        {"family": "duplicates", "cause": "duplicate_rows_in_file", "table": "order_items"},
        "quality_checks", ["unique_stg_order_items_order_item_id"],
        _double("order_items", every=20),
        true_fixes=[_redeliver("order_items"),
                    [("deduplicate_on_ingest", {"table": "order_items", "key": "order_item_id"})]],
        decoy_fixes=[RERUN, [("deduplicate_on_ingest", {"table": "orders", "key": "order_id"})]],
    ),
    Incident(
        "D3", "duplicates", "yesterday's orders re-sent inside today's orders file",
        {"family": "duplicates", "cause": "stale_rows_resent", "table": "orders"},
        "quality_checks", ["unique_stg_orders_order_id", "row_count_trend:raw.orders"],
        _append_stale_orders,
        true_fixes=[_redeliver("orders")],
        decoy_fixes=[[("deduplicate_on_ingest", {"table": "orders", "key": "order_id"})], RERUN],
    ),
    # -- failed jobs
    Incident(
        "F1", "failed_job", "bad deploy: fct_orders.sql selects a column that does not exist",
        {"family": "failed_job", "cause": "bad_model_deploy", "model": "fct_orders"},
        "transform", ["fct_orders", "order_total"],
        _edit_model("models/marts/fct_orders.sql", "o.order_amount,", "o.order_total as order_amount,"),
        true_fixes=[[("revert_model_change", {"model": "fct_orders"})]],
        decoy_fixes=[[("revert_model_change", {"model": "stg_orders"})], RERUN],
    ),
    Incident(
        "F2", "failed_job", "bad deploy: stg_orders renamed order_amount, breaking every downstream mart",
        {"family": "failed_job", "cause": "bad_model_deploy", "model": "stg_orders"},
        # The view still builds; tests on the vanished column error and dbt skips every downstream mart,
        # so the run fails at quality_checks, far from the model that changed.
        "quality_checks", ["order_amount", "freshness:marts.fct_orders"],
        _edit_model("models/staging/stg_orders.sql", "order_total as order_amount", "order_total as amount_usd"),
        true_fixes=[[("revert_model_change", {"model": "stg_orders"})]],
        decoy_fixes=[[("revert_model_change", {"model": "fct_orders"})], RERUN],
    ),
    Incident(
        "F3", "failed_job", "orders file truncated mid-write (export crashed)",
        {"family": "failed_job", "cause": "corrupt_landing_file", "table": "orders"},
        "ingest", ["failed loading raw.orders"],
        _truncate("orders", 0.6),
        true_fixes=[_redeliver("orders")],
        decoy_fixes=[RERUN, [("deduplicate_on_ingest", {"table": "orders", "key": "order_id"})]],
    ),
    # -- row-count drops (silent: nothing errors; only volume checks notice)
    Incident(
        "R1", "row_count_drop", "partial export: only orders placed before 09:00 were delivered",
        {"family": "row_count_drop", "cause": "partial_export", "table": "orders"},
        "quality_checks", ["row_count_trend:raw.orders", "row_count_trend:raw.order_items"],
        lambda paths, day: _keep_orders(paths, day, lambda r: int(r["order_ts"][11:13]) < 9),
        true_fixes=[_redeliver("orders", "order_items", "payments")],
        decoy_fixes=[RERUN, _redeliver("orders")],
    ),
    Incident(
        "R2", "row_count_drop", "upstream filter bug: all 'web' channel orders missing from the export",
        {"family": "row_count_drop", "cause": "filtered_export", "table": "orders"},
        "quality_checks", ["row_count_trend:raw.orders"],
        lambda paths, day: _keep_orders(paths, day, lambda r: r["channel"] != "web"),
        true_fixes=[_redeliver("orders", "order_items", "payments")],
        decoy_fixes=[RERUN],
    ),
    # -- more schema changes, missing partitions, duplicates, failed jobs and row-count drops
    Incident(
        "S6", "schema_change", "payments.amount renamed to amount_usd upstream",
        {"family": "schema_change", "cause": "column_renamed", "table": "payments", "column": "amount"},
        "ingest", ["missing columns ['amount']", "unexpected columns ['amount_usd']"],
        _upstream({"kind": "rename_column", "table": "payments", "from": "amount", "to": "amount_usd"}),
        true_fixes=[[("apply_column_mapping",
                      {"table": "payments", "source_column": "amount_usd", "target_column": "amount"})]],
        decoy_fixes=[_redeliver("payments"), RERUN],
    ),
    Incident(
        "S7", "schema_change", "order_items.quantity now exported with a unit suffix ('2 units')",
        {"family": "schema_change", "cause": "column_type_changed", "table": "order_items", "column": "quantity"},
        "ingest", ["failed loading raw.order_items", "units"],
        _upstream({"kind": "append_suffix", "table": "order_items", "column": "quantity", "suffix": " units"}),
        true_fixes=[[("escalate_to_owner",
                      {"owner": "upstream_app_team",
                       "summary": "order_items export now writes quantity with a unit suffix (e.g. '2 units')"})]],
        decoy_fixes=[[("set_column_parser",
                       {"table": "order_items", "column": "quantity", "parser": "strip_currency"})],
                     _redeliver("order_items"), RERUN],
        outcome="escalate",
    ),
    Incident(
        "S8", "schema_change", "customers gained a new upstream column marketing_opt_in",
        {"family": "schema_change", "cause": "column_added", "table": "customers", "column": "marketing_opt_in"},
        "ingest", ["unexpected columns ['marketing_opt_in']"],
        _upstream({"kind": "add_column", "table": "customers", "column": "marketing_opt_in",
                   "values": ["true", "false", "false", "true", "false"]}),
        true_fixes=[[("ignore_extra_columns", {"table": "customers", "columns": ["marketing_opt_in"]})]],
        decoy_fixes=[_redeliver("customers")],
    ),
    Incident(
        "M4", "missing_partition", "customers file for the day never arrived",
        {"family": "missing_partition", "cause": "file_missing", "table": "customers"},
        "ingest", ["landing file missing", "customers"],
        _delete("customers"), true_fixes=[_redeliver("customers")], decoy_fixes=[RERUN],
    ),
    Incident(
        "D4", "duplicates", "payments export retried and appended: every payment appears twice",
        {"family": "duplicates", "cause": "duplicate_rows_in_file", "table": "payments"},
        "quality_checks", ["unique_stg_payments_payment_id", "assert_payments_match_orders"],
        _double("payments"),
        true_fixes=[_redeliver("payments"), [("deduplicate_on_ingest", {"table": "payments", "key": "payment_id"})]],
        decoy_fixes=[RERUN, [("deduplicate_on_ingest", {"table": "orders", "key": "order_id"})]],
    ),
    Incident(
        "F4", "failed_job", "bad deploy: dim_customers.sql selects a column that does not exist",
        {"family": "failed_job", "cause": "bad_model_deploy", "model": "dim_customers"},
        "transform", ["dim_customers", "country_iso"],
        _edit_model("models/marts/dim_customers.sql", "c.country,", "c.country_iso,"),
        true_fixes=[[("revert_model_change", {"model": "dim_customers"})]],
        decoy_fixes=[[("revert_model_change", {"model": "stg_customers"})], RERUN],
    ),
    Incident(
        "F5", "failed_job", "bad deploy: stg_payments renamed amount, breaking fct_orders downstream",
        {"family": "failed_job", "cause": "bad_model_deploy", "model": "stg_payments"},
        # Like F2: the view still builds, the not_null test on the vanished column errors, and dbt skips
        # fct_orders (which reads stg_payments), so the run fails at quality_checks with a stale mart.
        "quality_checks", ["not_null_stg_payments_amount", "freshness:marts.fct_orders"],
        _edit_model("models/staging/stg_payments.sql", "    amount,", "    amount as amount_usd,"),
        true_fixes=[[("revert_model_change", {"model": "stg_payments"})]],
        decoy_fixes=[[("revert_model_change", {"model": "fct_orders"})], RERUN],
    ),
    Incident(
        "R3", "row_count_drop", "upstream filter bug: only 'web' orders delivered (app, marketplace missing)",
        {"family": "row_count_drop", "cause": "filtered_export", "table": "orders"},
        "quality_checks", ["row_count_trend:raw.orders"],
        lambda paths, day: _keep_orders(paths, day, lambda r: r["channel"] == "web"),
        true_fixes=[_redeliver("orders", "order_items", "payments")],
        decoy_fixes=[RERUN, _redeliver("orders")],
    ),
]  # fmt: skip

BY_ID = {i.id: i for i in INCIDENTS}

# Fixed before any agent run: dev incidents may be used to tune prompts; held-out ones only for the final eval.
DEV_SET = ["S1", "M2", "D1", "F1", "R1"]
HELD_OUT = [i.id for i in INCIDENTS if i.id not in DEV_SET]


def resolve_fix(fix: Fix, day: date) -> Fix:
    """Fill the {day} placeholder in action params."""
    out = []
    for name, params in fix:
        out.append((name, {k: (v.replace("{day}", day.isoformat()) if isinstance(v, str) else v)
                           for k, v in params.items()}))  # fmt: skip
    return out
