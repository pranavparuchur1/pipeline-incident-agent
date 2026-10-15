"""Investigation tools and their permission layer, exercised against copies of the baseline sandbox."""

import json
import shutil
from pathlib import Path

import pytest

from pipeline_incident_agent.incidents.catalog import BY_ID
from pipeline_incident_agent.sandbox.config import INCIDENT_DAY, REPO_ROOT, SandboxPaths
from pipeline_incident_agent.sandbox.pipeline import run_pipeline
from pipeline_incident_agent.sandbox.remediation import apply_action
from pipeline_incident_agent.tools import investigate as inv
from pipeline_incident_agent.tools.investigate import ToolError
from pipeline_incident_agent.tools.registry import ROLES, PermissionDenied, Trace, call_tool
from pipeline_incident_agent.tools.runbooks import RunbookIndex

BASELINE = REPO_ROOT / "sandbox" / "snapshots" / "baseline"
DAY = INCIDENT_DAY.isoformat()

if not BASELINE.exists():
    pytest.skip("baseline snapshot not present", allow_module_level=True)


def _copy_baseline(factory: pytest.TempPathFactory, name: str) -> SandboxPaths:
    dest = factory.mktemp(name) / "state"
    shutil.copytree(BASELINE, dest)
    return SandboxPaths(dest)


@pytest.fixture(scope="session")
def baseline_paths(tmp_path_factory) -> SandboxPaths:
    return _copy_baseline(tmp_path_factory, "baseline")


_INCIDENTS: dict[str, SandboxPaths] = {}


@pytest.fixture(scope="session")
def incident_sandbox(tmp_path_factory):
    def get(incident_id: str) -> SandboxPaths:
        if incident_id not in _INCIDENTS:
            paths = _copy_baseline(tmp_path_factory, f"inc_{incident_id}")
            BY_ID[incident_id].inject(paths, INCIDENT_DAY)
            run_pipeline(paths, INCIDENT_DAY)
            _INCIDENTS[incident_id] = paths
        return _INCIDENTS[incident_id]

    return get


@pytest.fixture
def trace(tmp_path) -> Trace:
    return Trace(tmp_path / "trace")


# ------------------------------------------------------------------ 1. S1 schema change


def test_s1_job_status_and_logs(incident_sandbox):
    paths = incident_sandbox("S1")
    status = inv.get_job_status(paths)
    latest = status.data[0]
    assert latest["status"] != "SUCCESS"
    assert any("order_total" in f for f in latest["failures"])
    logs = inv.search_logs(paths, query="ERROR")
    assert logs.data
    assert any(d["id"].startswith("log:") and "total_amount" in d["line"] for d in logs.data)
    assert any(e.id.startswith("log:") for e in logs.evidence)


def test_s1_landing_file_mismatch_then_fixed(incident_sandbox, tmp_path_factory):
    paths = incident_sandbox("S1")
    res = inv.inspect_landing_file(paths, "orders", DAY)
    assert res.data["missing_contract_columns"] == ["order_total"]
    assert res.data["columns_not_in_contract"] == ["total_amount"]
    assert [e.id for e in res.evidence] == [f"landing:orders@{DAY}"]
    assert "CONTRACT MISMATCH" in res.summary

    # apply the fix on a private copy so the shared incident sandbox stays broken
    fixed = SandboxPaths(tmp_path_factory.mktemp("s1fix") / "state")
    shutil.copytree(paths.root, fixed.root)
    apply_action(
        fixed,
        "apply_column_mapping",
        {"table": "orders", "source_column": "total_amount", "target_column": "order_total"},
    )
    after = inv.inspect_landing_file(fixed, "orders", DAY)
    assert after.data["missing_contract_columns"] == []
    assert after.data["columns_not_in_contract"] == []
    assert "MISMATCH" not in after.summary


# ------------------------------------------------------------------ 2. F1 bad deploy


def test_f1_changed_models(incident_sandbox):
    paths = incident_sandbox("F1")
    assert inv.list_changed_models(paths).data["changed_models"] == ["fct_orders"]
    changed = inv.get_dbt_model(paths, "fct_orders")
    assert changed.data["changed_vs_deployed"] is True
    assert "order_total" in changed.data["diff"]
    stg = inv.get_dbt_model(paths, "stg_orders")
    assert stg.data["changed_vs_deployed"] is False
    assert "fct_orders" in stg.data["downstream_models"]


# ------------------------------------------------------------------ 3. R2 row count drop


def test_r2_quality_checks_and_chart(incident_sandbox, trace):
    paths = incident_sandbox("R2")
    dqr = inv.run_data_quality_checks(paths, DAY)
    failing = {c["check_id"] for c in dqr.data["live"] if c["status"] == "fail"}
    failing |= {c["check_id"] for c in dqr.data["latest_run"] if c["status"] == "fail"}
    assert "row_count_trend:raw.orders" in failing

    chart = call_tool(paths, trace, "worker", "render_chart", {"table": "orders", "by": "channel"})
    assert chart.ok and chart.images
    png = Path(chart.images[0])
    assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    series = json.loads(chart.evidence[0].text)
    assert "web" not in series or DAY not in series["web"] or series["web"][DAY] == 0
    assert any(DAY in pts for k, pts in series.items() if k != "web")


def test_render_chart_rejects_by_hour_on_payments(baseline_paths, tmp_path):
    with pytest.raises(ToolError):
        inv.render_chart(baseline_paths, "payments", by="hour", out_dir=tmp_path)


# ------------------------------------------------------------------ 4. query_table guards


def test_query_table_select_works(baseline_paths):
    res = inv.query_table(baseline_paths, "select count(*) as n from raw.orders")
    assert res.ok and res.data["rows"][0][0] > 0
    assert res.evidence[0].id.startswith("sql:")


@pytest.mark.parametrize(
    "sql",
    ["delete from raw.orders", "select 1; select 2", "attach 'x.db'", "create table t as select 1"],
)
def test_query_table_rejects(baseline_paths, sql):
    with pytest.raises(ToolError):
        inv.query_table(baseline_paths, sql)


def test_query_table_no_file_access(baseline_paths):
    landing = next(baseline_paths.landing.glob("orders/**/*.csv")).resolve()
    header = landing.read_text(encoding="utf-8").splitlines()[0]
    for sql in (
        "select * from read_csv('x.csv')",
        f"select * from read_csv_auto('{landing.as_posix()}')",
    ):
        try:
            res = inv.query_table(baseline_paths, sql)
        except ToolError:
            continue
        assert res.ok is False
        assert res.data is None
        assert header not in res.summary


# ------------------------------------------------------------------ 5. tools cannot reach internals


def test_inspect_schema_rejects_internal_relations(baseline_paths):
    for rel in ("upstream.changes", "information_schema.tables", "raw.orders; drop", "", None):
        with pytest.raises(ToolError):
            inv.inspect_schema(baseline_paths, rel)


def test_get_dbt_model_rejects_traversal(baseline_paths):
    for m in ("../x", "..", "a/b", "", "nonexistent_model"):
        with pytest.raises(ToolError):
            inv.get_dbt_model(baseline_paths, m)


def test_inspect_landing_file_rejects_bad_input(baseline_paths):
    with pytest.raises(ToolError):
        inv.inspect_landing_file(baseline_paths, "products", "2026-09-01")
    for day in ("yesterday", "../../etc", "2026-13-40", ""):
        with pytest.raises(ToolError):
            inv.inspect_landing_file(baseline_paths, "orders", day)


# ------------------------------------------------------------------ 6. permissions


def test_worker_cannot_propose_fix(baseline_paths, trace):
    with pytest.raises(PermissionDenied):
        call_tool(baseline_paths, trace, "worker", "propose_fix", {"actions": [], "rationale": "", "evidence_ids": []})
    assert any(e["type"] == "denied" and e["tool"] == "propose_fix" for e in trace.events())


def test_unknown_role_denied(baseline_paths, trace):
    with pytest.raises(PermissionDenied):
        call_tool(baseline_paths, trace, "intern", "get_ingest_config", {})


def test_role_tables():
    assert "propose_fix" not in ROLES["worker"]
    assert "propose_fix" in ROLES["master"]
    assert set(ROLES["worker"]) < set(ROLES["master"])


# ------------------------------------------------------------------ 7. propose_fix

GOOD = [{"action": "rerun_only", "params": {}}]


def _seed(paths, trace) -> str:
    res = call_tool(paths, trace, "master", "get_ingest_config", {})
    return res.evidence[0].id


def _propose(paths, trace, **kw):
    args = {"actions": GOOD, "rationale": "r", "evidence_ids": ["config:ingest"]} | kw
    return call_tool(paths, trace, "master", "propose_fix", args)


def test_propose_fix_rejections(baseline_paths, trace):
    assert _seed(baseline_paths, trace) == "config:ingest"
    bad = [
        {"actions": [{"action": "drop_database", "params": {}}]},
        {"actions": [{"action": "apply_column_mapping", "params": {"table": "orders"}}]},
        {"actions": GOOD, "evidence_ids": []},
        {"actions": GOOD, "evidence_ids": ["log:99999"]},
        {"actions": []},
    ]
    for kw in bad:
        res = _propose(baseline_paths, trace, **kw)
        assert res.ok is False, kw
    assert not [e for e in trace.events() if e["type"] == "proposal"]


def test_propose_fix_accepts_and_does_not_touch_sandbox(baseline_paths, trace):
    _seed(baseline_paths, trace)
    cfg_before = baseline_paths.ingest_config.exists()
    landing = baseline_paths.landing_file("orders", INCIDENT_DAY)
    landing_before = landing.read_bytes() if landing.exists() else None
    mapping = {"table": "orders", "source_column": "total_amount", "target_column": "order_total"}
    res = _propose(baseline_paths, trace, actions=[{"action": "apply_column_mapping", "params": mapping}])
    assert res.ok
    proposals = [e for e in trace.events() if e["type"] == "proposal"]
    assert len(proposals) == 1 and proposals[0]["evidence_ids"] == ["config:ingest"]
    assert baseline_paths.ingest_config.exists() == cfg_before
    assert not baseline_paths.ingest_config.exists() or "total_amount" not in baseline_paths.ingest_config.read_text()
    assert (landing.read_bytes() if landing.exists() else None) == landing_before


# ------------------------------------------------------------------ 8. runbooks


def test_runbook_index_loads():
    idx = RunbookIndex.load()
    assert len(idx.sections) > 10
    assert all(s.id.startswith("runbook:") for s in idx.sections)


def test_search_runbooks_ranking(baseline_paths):
    res = inv.search_runbooks(baseline_paths, "duplicate order_id unique test")
    assert res.data[0]["id"].startswith("runbook:duplicates")
    assert res.evidence[0].id == res.data[0]["id"]
    res = inv.search_runbooks(baseline_paths, "upstream renamed column missing columns unexpected columns")
    assert any(h["id"].startswith("runbook:schema_changes") for h in res.data)


# ------------------------------------------------------------------ 9. trace


def test_trace_records_tool_calls(baseline_paths, trace):
    r1 = call_tool(baseline_paths, trace, "worker", "inspect_schema", {"relation": "raw.orders"})
    r2 = call_tool(baseline_paths, trace, "worker", "get_ingest_config", {})
    calls = [e for e in trace.events() if e["type"] == "tool_call"]
    assert [c["tool"] for c in calls] == ["inspect_schema", "get_ingest_config"]
    assert [ev["id"] for ev in calls[0]["evidence"]] == [e.id for e in r1.evidence]
    assert [ev["id"] for ev in calls[1]["evidence"]] == [e.id for e in r2.evidence]
    ev = trace.evidence()
    assert ev["schema:raw.orders"] == r1.evidence[0].text
    assert ev["config:ingest"] == r2.evidence[0].text


def test_call_tool_converts_tool_errors(baseline_paths, trace):
    res = call_tool(baseline_paths, trace, "worker", "inspect_schema", {"relation": "upstream.changes"})
    assert res.ok is False
    assert trace.events()[-1]["ok"] is False
