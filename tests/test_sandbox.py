from datetime import date, timedelta

import duckdb
import pytest

from pipeline_incident_agent.sandbox import dq
from pipeline_incident_agent.sandbox.config import HISTORY_START, SandboxPaths
from pipeline_incident_agent.sandbox.generator import COLUMNS, generate_day, generate_products, orders_for_day
from pipeline_incident_agent.sandbox.ingest import IngestError, load_partition
from pipeline_incident_agent.sandbox.runlog import RunLog
from pipeline_incident_agent.sandbox.snapshot import fingerprint, restore, snapshot

D1, D2 = HISTORY_START, HISTORY_START + timedelta(days=1)


@pytest.fixture
def paths(tmp_path):
    p = SandboxPaths(tmp_path / "state")
    p.ensure()
    generate_products(p)
    for d in (D1, D2):
        generate_day(p, d, HISTORY_START)
    return p


def test_generator_is_deterministic(paths, tmp_path):
    other = SandboxPaths(tmp_path / "other")
    other.ensure()
    generate_day(other, D2, HISTORY_START)
    for t in ("customers", "orders", "order_items", "payments"):
        assert paths.landing_file(t, D2).read_bytes() == other.landing_file(t, D2).read_bytes()


def test_generator_ids_unique_across_days():
    a, b = orders_for_day(D1, HISTORY_START), orders_for_day(D2, HISTORY_START)
    ids = [o["order_id"] for o in a["orders"] + b["orders"]]
    items = [i["order_item_id"] for i in a["order_items"] + b["order_items"]]
    assert len(ids) == len(set(ids)) and len(items) == len(set(items))


def test_order_totals_match_items_and_payments():
    day = orders_for_day(D2, HISTORY_START)
    by_order: dict[int, float] = {}
    for i in day["order_items"]:
        by_order[i["order_id"]] = by_order.get(i["order_id"], 0) + i["quantity"] * float(i["unit_price"])
    for o in day["orders"]:
        assert abs(float(o["order_total"]) - by_order[o["order_id"]]) < 0.01
    paid = {p["order_id"] for p in day["payments"]}
    assert all((o["order_id"] in paid) == (o["status"] != "cancelled") for o in day["orders"])


def test_load_is_idempotent(paths):
    load_partition(paths, D1, "r1")
    first = load_partition(paths, D1, "r2")
    with duckdb.connect(str(paths.warehouse)) as con:
        n = con.execute("select count(*) from raw.orders where _dt = ?", [D1]).fetchone()[0]
    assert n == next(r.rows for r in first if r.table == "orders")


def test_renamed_column_fails_loudly(paths):
    f = paths.landing_file("orders", D1)
    f.write_text(f.read_text().replace("order_total", "total_amount", 1))
    with pytest.raises(IngestError, match=r"missing columns \['order_total'\].*unexpected columns \['total_amount'\]"):
        load_partition(paths, D1, "r1")


def test_missing_landing_file_fails_loudly(paths):
    paths.landing_file("payments", D1).unlink()
    with pytest.raises(IngestError, match="landing file missing"):
        load_partition(paths, D1, "r1")


def test_bad_value_rolls_back(paths):
    load_partition(paths, D1, "r1")
    f = paths.landing_file("orders", D1)
    lines = f.read_text().splitlines()
    cells = lines[1].split(",")
    cells[COLUMNS["orders"].index("order_total")] = "$12.00"
    lines[1] = ",".join(cells)
    f.write_text("\n".join(lines) + "\n")
    with pytest.raises(IngestError):
        load_partition(paths, D1, "r2")
    with duckdb.connect(str(paths.warehouse)) as con:  # the previous good load is still there
        assert con.execute("select count(*) from raw.orders where _dt = ?", [D1]).fetchone()[0] > 0


def test_runlog_one_line_per_event(tmp_path):
    log = RunLog(tmp_path / "p.log", "abcdef1234", "2026-09-01")
    log.error("ingest", "boom\n  at line 3\n")
    (line,) = (tmp_path / "p.log").read_text().splitlines()
    assert "ERROR [run=abcdef12 partition=2026-09-01 step=ingest] boom | at line 3" in line


def test_row_count_trend_flags_drop():
    con = duckdb.connect()
    con.execute("create schema raw")
    con.execute("create table raw.orders (_dt date)")
    for i in range(7):
        con.execute("insert into raw.orders select ?::date from range(100)", [date(2026, 9, 1) + timedelta(days=i)])
    day = date(2026, 9, 8)
    con.execute("insert into raw.orders select ?::date from range(30)", [day])
    for t in ("order_items", "payments"):
        con.execute(f"create table raw.{t} as select * from raw.orders")
    (orders_check,) = [c for c in dq.row_count_trend(con, day) if c.check_id.endswith("raw.orders")]
    assert orders_check.status == "fail" and "30%" in orders_check.observed


def test_snapshot_restore_and_fingerprint(paths):
    load_partition(paths, D1, "r1")
    before = fingerprint(paths, ("raw",))
    snapshot(paths, "s1")
    load_partition(paths, D2, "r2")
    assert fingerprint(paths, ("raw",)) != before
    restore(paths, "s1")
    assert fingerprint(paths, ("raw",)) == before


def test_fingerprint_ignores_run_id(paths):
    load_partition(paths, D1, "r1")
    a = fingerprint(paths, ("raw",))
    load_partition(paths, D1, "different-run")
    assert fingerprint(paths, ("raw",)) == a
