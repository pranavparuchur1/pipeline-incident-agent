"""The remediation allow-list: the only ways anything (including the agent) may change the sandbox.

Each action is narrow, validates its parameters strictly, touches one well-defined piece of
state, and returns a human-readable record of what it changed. Anything not listed here is
impossible: there is no "run arbitrary SQL" or "skip the failing test" action.

Applying an action never reruns the pipeline. Verification (rerun + checks + comparison with the
known-good state) is a separate step, so an action can't mark its own work as done.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date

from .config import DBT_PROJECT_DIR, EVENT_TABLES, SandboxPaths
from .ingest import PARSERS, RAW_CONTRACT, load_ingest_config
from .upstream import redeliver

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
# Dry run: every validation runs, the three places that write state are skipped. Lets propose_fix reject
# bad parameter values at proposal time, while the agent can still correct them.
_DRY_RUN: ContextVar[bool] = ContextVar("remediation_dry_run", default=False)


class RemediationError(ValueError):
    pass


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    params: dict[str, str]  # param -> human description (used later to build the tool schema)
    changes_state: bool
    apply: Callable[..., str]


def _ident(value: str, what: str) -> str:
    if not isinstance(value, str) or not _IDENT.match(value):
        raise RemediationError(f"{what} must be a lowercase identifier, got {value!r}")
    return value


def _table(value: str, *, event_only: bool = False) -> str:
    allowed = EVENT_TABLES if event_only else tuple(RAW_CONTRACT)
    if value not in allowed:
        raise RemediationError(f"unknown table {value!r}; expected one of {list(allowed)}")
    return value


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as e:
        raise RemediationError(f"day must be YYYY-MM-DD, got {value!r}") from e


def _update_ingest_config(paths: SandboxPaths, table: str, fn: Callable[[dict], None]) -> None:
    if _DRY_RUN.get():
        return
    cfg = load_ingest_config(paths)
    fn(cfg.setdefault(table, {}))
    paths.ingest_config.parent.mkdir(parents=True, exist_ok=True)
    paths.ingest_config.write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")


def request_upstream_redelivery(paths: SandboxPaths, tables: list[str], day: str) -> str:
    d = _day(day)
    if not tables or not isinstance(tables, list):
        raise RemediationError("tables must be a non-empty list")
    checked = [_table(t, event_only=True) for t in tables]
    if _DRY_RUN.get():
        return f"would ask upstream to re-export {checked} for {d}"
    counts = {t: redeliver(paths, t, d) for t in checked}
    return f"upstream re-exported {', '.join(f'{t} ({n} rows)' for t, n in counts.items())} for {d}"


def apply_column_mapping(paths: SandboxPaths, table: str, source_column: str, target_column: str) -> str:
    _table(table)
    src, dst = _ident(source_column, "source_column"), _ident(target_column, "target_column")
    if dst not in RAW_CONTRACT[table]:
        raise RemediationError(f"target_column {dst!r} is not in the raw.{table} contract")
    _update_ingest_config(paths, table, lambda c: c.setdefault("rename", {}).__setitem__(src, dst))
    return f"ingest now reads raw.{table}.{dst} from upstream column {src!r}"


def set_column_parser(paths: SandboxPaths, table: str, column: str, parser: str) -> str:
    _table(table)
    col = _ident(column, "column")
    if col not in RAW_CONTRACT[table]:
        raise RemediationError(f"column {col!r} is not in the raw.{table} contract")
    if parser not in PARSERS:
        raise RemediationError(f"unknown parser {parser!r}; expected one of {list(PARSERS)}")
    _update_ingest_config(paths, table, lambda c: c.setdefault("parsers", {}).__setitem__(col, parser))
    return f"ingest now parses raw.{table}.{col} with {parser}"


def ignore_extra_columns(paths: SandboxPaths, table: str, columns: list[str]) -> str:
    _table(table)
    cols = [_ident(c, "column") for c in columns or []]
    if not cols:
        raise RemediationError("columns must be a non-empty list")
    if clash := [c for c in cols if c in RAW_CONTRACT[table]]:
        raise RemediationError(f"refusing to ignore contract columns {clash}")

    def upd(c: dict) -> None:
        c["ignore_columns"] = sorted(set(c.get("ignore_columns", [])) | set(cols))

    _update_ingest_config(paths, table, upd)
    return f"ingest now ignores upstream columns {cols} on raw.{table}"


def deduplicate_on_ingest(paths: SandboxPaths, table: str, key: str) -> str:
    _table(table, event_only=True)
    k = _ident(key, "key")
    if k not in RAW_CONTRACT[table]:
        raise RemediationError(f"key {k!r} is not a raw.{table} column")
    _update_ingest_config(paths, table, lambda c: c.__setitem__("dedupe_key", k))
    return f"ingest now keeps one row per {k} when loading raw.{table}"


def revert_model_change(paths: SandboxPaths, model: str) -> str:
    """Restore one dbt model to the deployed (repo) version, like reverting a bad commit."""
    m = _ident(model, "model")
    matches = list(DBT_PROJECT_DIR.glob(f"models/**/{m}.sql"))
    if len(matches) != 1:
        raise RemediationError(f"unknown dbt model {m!r}")
    rel = matches[0].relative_to(DBT_PROJECT_DIR)
    if _DRY_RUN.get():
        return f"would revert {rel.as_posix()}"
    current = paths.dbt_project / rel
    changed = not current.exists() or current.read_bytes() != matches[0].read_bytes()
    shutil.copyfile(matches[0], current)
    return f"reverted {rel.as_posix()} to the deployed version" + ("" if changed else " (it was unchanged)")


def rerun_only(paths: SandboxPaths) -> str:
    return "no change; the pipeline will simply be rerun"


def escalate_to_owner(paths: SandboxPaths, owner: str, summary: str) -> str:
    if owner not in ("upstream_app_team", "analytics_engineering", "data_platform"):
        raise RemediationError(f"unknown owner {owner!r}")
    return f"escalated to {owner}: {summary[:300]}"


ACTIONS: dict[str, Action] = {
    a.name: a
    for a in (
        Action("request_upstream_redelivery", "Ask the upstream system to re-export these tables for a day.",
               {"tables": "list of event tables", "day": "YYYY-MM-DD"}, True, request_upstream_redelivery),
        Action("apply_column_mapping", "Read a renamed upstream column into its contract column at ingest.",
               {"table": "raw table", "source_column": "upstream name", "target_column": "contract name"},
               True, apply_column_mapping),
        Action("set_column_parser", "Parse a reformatted upstream value at ingest.",
               {"table": "raw table", "column": "contract column", "parser": f"one of {list(PARSERS)}"},
               True, set_column_parser),
        Action("ignore_extra_columns", "Ignore new upstream columns that are not part of the contract.",
               {"table": "raw table", "columns": "list of upstream column names"}, True, ignore_extra_columns),
        Action("deduplicate_on_ingest", "Keep one row per key when loading a table.",
               {"table": "event table", "key": "unique key column"}, True, deduplicate_on_ingest),
        Action("revert_model_change", "Restore one dbt model to the deployed version.",
               {"model": "dbt model name"}, True, revert_model_change),
        Action("rerun_only", "Change nothing; just rerun the pipeline.", {}, False, rerun_only),
        Action("escalate_to_owner", "Hand the incident to a human team; change nothing.",
               {"owner": "upstream_app_team | analytics_engineering | data_platform", "summary": "what and why"},
               False, escalate_to_owner),
    )
}  # fmt: skip


def validate_action(name: str, params: dict) -> str:
    """Run every check an action would run, without changing anything. Raises RemediationError."""
    token = _DRY_RUN.set(True)
    try:
        return apply_action(SandboxPaths(DBT_PROJECT_DIR.parent / "_dry_run_"), name, params)
    finally:
        _DRY_RUN.reset(token)


def apply_action(paths: SandboxPaths, name: str, params: dict) -> str:
    action = ACTIONS.get(name)
    if action is None:
        raise RemediationError(f"action {name!r} is not on the allow-list")
    unknown = set(params) - set(action.params)
    missing = set(action.params) - set(params)
    if unknown or missing:
        raise RemediationError(f"bad params for {name}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    return action.apply(paths, **params)
