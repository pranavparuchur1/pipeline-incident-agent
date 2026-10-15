from datetime import date, timedelta

import duckdb
import pytest

from pipeline_incident_agent.incidents.catalog import BY_ID, INCIDENTS
from pipeline_incident_agent.sandbox import remediation, upstream
from pipeline_incident_agent.sandbox.config import DBT_PROJECT_DIR, HISTORY_START, SandboxPaths
from pipeline_incident_agent.sandbox.generator import COLUMNS, generate_day, generate_products
from pipeline_incident_agent.sandbox.ingest import IngestError, load_partition
from pipeline_incident_agent.sandbox.remediation import ACTIONS, RemediationError, apply_action

D1 = HISTORY_START
D2 = HISTORY_START + timedelta(days=1)


def _make(root) -> SandboxPaths:
    p = SandboxPaths(root)
    p.ensure()
    generate_products(p)
    for d in (D1, D2):
        generate_day(p, d, HISTORY_START)
    return p


def _orders_stats(p: SandboxPaths):
    load_partition(p, D2, "r")
    with duckdb.connect(str(p.warehouse)) as con:
        return con.execute("select count(*), sum(order_total) from raw.orders where _dt = ?", [D2]).fetchone()


@pytest.fixture
def paths(tmp_path):
    return _make(tmp_path / "state")


@pytest.fixture
def clean_orders(tmp_path):
    """(row count, sum of order_total) of a clean load of D2 in a separate sandbox."""
    return _orders_stats(_make(tmp_path / "clean"))


# ---------------------------------------------------------------- 1. upstream.apply_changes

ROWS = [{"a": "1", "b": "10.50", "c": "x"}, {"a": "2", "b": "20.00", "c": "y"}]
COLS = ["a", "b", "c"]
DAY = date(2026, 10, 1)


def _apply(change, day=DAY, table="t"):
    return upstream.apply_changes(table, day, ROWS, COLS, [{"table": "t", "effective": "2026-10-01", **change}])


def test_apply_rename():
    rows, cols = _apply({"kind": "rename_column", "from": "b", "to": "z"})
    assert cols == ["a", "z", "c"] and rows[0]["z"] == "10.50" and "b" not in rows[0]


def test_apply_add_column_cycles_values():
    rows, cols = _apply({"kind": "add_column", "column": "n", "values": ["p", "q", "r"]})
    assert cols == [*COLS, "n"] and [r["n"] for r in rows] == ["p", "q"]


def test_apply_drop_column():
    rows, cols = _apply({"kind": "drop_column", "column": "c"})
    assert cols == ["a", "b"] and all("c" not in r for r in rows)


def test_apply_format_currency():
    rows, cols = _apply({"kind": "format_currency", "column": "b"})
    assert [r["b"] for r in rows] == ["$10.50", "$20.00"] and cols == COLS


def test_apply_respects_effective_day_and_table_and_does_not_mutate_input():
    ch = {"kind": "drop_column", "column": "c"}
    assert _apply(ch, day=DAY - timedelta(days=1)) == (ROWS, COLS)
    assert _apply(ch, table="other") == (ROWS, COLS)
    _apply(ch)
    assert "c" in ROWS[0] and COLS == ["a", "b", "c"]


def test_add_change_rejects_unknown_kind(paths):
    with pytest.raises(ValueError, match="unknown upstream change kind"):
        upstream.add_change(paths, {"kind": "explode", "table": "orders", "effective": "2026-10-01"})
    assert upstream.load_changes(paths) == []


# ---------------------------------------------------------------- 2. redeliver


def test_redeliver_without_changes_is_byte_identical(paths):
    f = paths.landing_file("orders", D2)
    original = f.read_bytes()
    f.write_bytes(b"garbage")
    upstream.redeliver(paths, "orders", D2)
    assert f.read_bytes() == original


def test_redeliver_with_rename_reproduces_renamed_header(paths):
    change = {"kind": "rename_column", "table": "orders", "from": "order_total", "to": "total_amount"}
    upstream.add_change(paths, {**change, "effective": D2.isoformat()})
    upstream.redeliver(paths, "orders", D2)
    header = paths.landing_file("orders", D2).read_text().splitlines()[0].split(",")
    assert header == [("total_amount" if c == "order_total" else c) for c in COLUMNS["orders"]]


# ---------------------------------------------------------------- 3. ingest config end to end


def test_rename_incident_then_column_mapping(paths, clean_orders):
    BY_ID["S1"].inject(paths, D2)
    with pytest.raises(IngestError, match="total_amount"):
        load_partition(paths, D2, "r1")
    mapping = {"table": "orders", "source_column": "total_amount", "target_column": "order_total"}
    apply_action(paths, "apply_column_mapping", mapping)
    assert _orders_stats(paths) == clean_orders


def test_strip_currency_parser_after_s3(paths, clean_orders):
    BY_ID["S3"].inject(paths, D2)
    with pytest.raises(IngestError):
        load_partition(paths, D2, "r1")
    apply_action(paths, "set_column_parser", {"table": "orders", "column": "order_total", "parser": "strip_currency"})
    assert _orders_stats(paths) == clean_orders


def test_ignore_extra_columns_after_s4(paths, clean_orders):
    BY_ID["S4"].inject(paths, D2)
    with pytest.raises(IngestError, match="coupon_code"):
        load_partition(paths, D2, "r1")
    apply_action(paths, "ignore_extra_columns", {"table": "orders", "columns": ["coupon_code"]})
    assert _orders_stats(paths) == clean_orders


def test_deduplicate_on_ingest_after_d1(paths, clean_orders):
    BY_ID["D1"].inject(paths, D2)
    assert _orders_stats(paths)[0] == 2 * clean_orders[0]
    apply_action(paths, "deduplicate_on_ingest", {"table": "orders", "key": "order_id"})
    assert _orders_stats(paths)[0] == clean_orders[0]


def test_dropped_column_cannot_be_fixed_by_redelivery(paths):
    BY_ID["S5"].inject(paths, D2)
    apply_action(paths, "request_upstream_redelivery", {"tables": ["payments"], "day": D2.isoformat()})
    with pytest.raises(IngestError, match=r"missing columns \['method'\]"):
        load_partition(paths, D2, "r1")


# ---------------------------------------------------------------- 4. validation

MAP = "apply_column_mapping"
BAD_CALLS = [
    ("nope", {}, "not on the allow-list"),
    ("rerun_only", {"x": 1}, "bad params"),
    (MAP, {"table": "orders", "source_column": "a"}, "missing"),
    (MAP, {"table": "orders", "source_column": "x; drop table", "target_column": "order_total"}, "lowercase"),
    (MAP, {"table": "nosuch", "source_column": "a", "target_column": "order_total"}, "unknown table"),
    (MAP, {"table": "orders", "source_column": "a", "target_column": "nosuch"}, "not in the raw"),
    ("ignore_extra_columns", {"table": "orders", "columns": ["order_total"]}, "refusing to ignore"),
    ("ignore_extra_columns", {"table": "orders", "columns": []}, "non-empty"),
    ("set_column_parser", {"table": "orders", "column": "order_total", "parser": "eval"}, "unknown parser"),
    ("set_column_parser", {"table": "orders", "column": "nosuch", "parser": "strip_currency"}, "not in the raw"),
    ("deduplicate_on_ingest", {"table": "products", "key": "product_id"}, "unknown table"),
    ("revert_model_change", {"model": "no_such_model"}, "unknown dbt model"),
    ("request_upstream_redelivery", {"tables": ["orders"], "day": "10/01/2026"}, "YYYY-MM-DD"),
    ("request_upstream_redelivery", {"tables": [], "day": "2026-09-02"}, "non-empty"),
    ("escalate_to_owner", {"owner": "the_intern", "summary": "x"}, "unknown owner"),
]


@pytest.mark.parametrize(("name", "params", "match"), BAD_CALLS)
def test_invalid_actions_are_rejected_without_side_effects(paths, name, params, match):
    with pytest.raises(RemediationError, match=match):
        apply_action(paths, name, params)
    assert not paths.ingest_config.exists()


# ---------------------------------------------------------------- 5. revert_model_change


def test_revert_model_change_restores_and_reports_unchanged(paths):
    rel = "models/marts/fct_orders.sql"
    deployed = (DBT_PROJECT_DIR / rel).read_bytes()
    BY_ID["F1"].inject(paths, D2)
    assert (paths.dbt_project / rel).read_bytes() != deployed
    msg = apply_action(paths, "revert_model_change", {"model": "fct_orders"})
    assert (paths.dbt_project / rel).read_bytes() == deployed and "unchanged" not in msg
    assert apply_action(paths, "revert_model_change", {"model": "fct_orders"}).endswith("(it was unchanged)")


# ---------------------------------------------------------------- 6. catalog sanity


def test_incident_ids_unique():
    ids = [i.id for i in INCIDENTS]
    assert len(ids) == len(set(ids)) == len(BY_ID)


@pytest.mark.parametrize("inc", INCIDENTS, ids=lambda i: i.id)
def test_incident_fixes_reference_valid_actions(inc):
    assert inc.outcome in {"recover", "escalate"}
    assert inc.true_fixes
    for fix in inc.true_fixes + inc.decoy_fixes:
        for name, params in fix:
            assert name in ACTIONS, name
            assert set(params) == set(ACTIONS[name].params), (inc.id, name)


@pytest.mark.parametrize("inc", [i for i in INCIDENTS if i.outcome == "escalate"], ids=lambda i: i.id)
def test_escalate_incidents_only_use_non_state_changing_true_fixes(inc):
    assert all(not ACTIONS[name].changes_state for fix in inc.true_fixes for name, _ in fix)


def test_remediation_error_is_value_error():
    assert issubclass(remediation.RemediationError, ValueError)
