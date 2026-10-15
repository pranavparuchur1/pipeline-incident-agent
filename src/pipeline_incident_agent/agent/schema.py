"""Structured outputs: the master's diagnosis and a worker's findings.

The root-cause vocabulary is a fixed taxonomy (like an incident-classification scheme a team
would agree on), so a diagnosis can be graded mechanically against ground truth.
"""

from __future__ import annotations

FAMILIES = ["schema_change", "missing_partition", "duplicates", "failed_job", "row_count_drop", "other"]
CAUSES = [
    "column_renamed", "column_type_changed", "column_added", "column_dropped",
    "file_missing", "file_empty", "partial_export", "filtered_export",
    "duplicate_rows_in_file", "stale_rows_resent",
    "bad_model_deploy", "corrupt_landing_file", "transient_failure", "other",
]  # fmt: skip
WORKERS = {
    "log_analyst": "Read run history and the pipeline log: what failed, where, and the exact error lines.",
    "source_inspector": "Inspect what upstream delivered (landing files) against the raw contract and ingest config.",
    "data_profiler": "Profile the data: quality-check results, SQL queries, and row-count charts (read the images).",
    "change_auditor": "Find dbt models that differ from the deployed version and trace their downstream impact.",
    "runbook_researcher": "Find what the team's runbooks and architecture docs say about this kind of failure.",
}

_NULLABLE_STR = {"type": ["string", "null"]}
_CLAIM = {
    "type": "object",
    "properties": {"claim": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["claim", "evidence_ids"],
}
_OWNERS = ["upstream_app_team", "analytics_engineering", "data_platform", None]

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "two or three sentences for the incident channel"},
        "root_cause": {
            "type": "object",
            "properties": {
                "family": {"type": "string", "enum": FAMILIES},
                "cause": {"type": "string", "enum": CAUSES},
                "table": {**_NULLABLE_STR, "description": "affected raw/event table, if any"},
                "column": {**_NULLABLE_STR, "description": "affected contract column, if any"},
                "model": {**_NULLABLE_STR, "description": "affected dbt model, if any"},
                "explanation": {"type": "string"},
            },
            "required": ["family", "cause", "table", "column", "model", "explanation"],
        },
        "evidence": {
            "type": "array",
            "description": "each claim with the evidence ids that support it",
            "items": _CLAIM,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "decision": {"type": "string", "enum": ["fix", "escalate"],
                     "description": "fix = you called propose_fix; escalate = no safe automated fix"},
        "escalation_owner": {**_NULLABLE_STR, "enum": _OWNERS},
        "alternatives_considered": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "root_cause", "evidence", "confidence", "decision", "escalation_owner",
                 "alternatives_considered"],
}  # fmt: skip

WORKER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "direct answer to the task, 1-4 sentences"},
        "findings": {
            "type": "array",
            "items": _CLAIM,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["answer", "findings", "confidence"],
}  # fmt: skip
