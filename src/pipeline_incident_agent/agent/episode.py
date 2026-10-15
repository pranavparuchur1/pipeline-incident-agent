"""One incident, end to end: inject -> page the agent -> apply its proposal -> verify -> (retry) -> grade.

The verification loop is the point of the project. The agent never decides whether it succeeded:
the harness applies the proposed actions in the sandbox, reruns the pipeline, and compares every
table with the known-good reference. If that fails, the sandbox is rolled back to its pre-fix
state and the agent gets one more attempt with the verification result as feedback.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ..incidents.catalog import BY_ID, Incident
from ..incidents.harness import BASELINE, reference
from ..llm.claude_cli import ClaudeCLIError
from ..sandbox.config import INCIDENT_DAY, REPO_ROOT, SandboxPaths
from ..sandbox.pipeline import run_pipeline
from ..sandbox.remediation import ACTIONS, RemediationError, apply_action
from ..sandbox.snapshot import restore, snapshot
from ..sandbox.verify import failed_checks, run_log_lines, verify
from ..tools.registry import Trace
from .config import AgentConfig
from .master import run_master

RUNS_DIR = REPO_ROOT / "runs"


class InfrastructureError(RuntimeError):
    """The sandbox or the CLI failed; the episode is discarded, never graded."""


MAX_ATTEMPTS = 2


@dataclass
class Attempt:
    n: int
    diagnosis: dict | None
    proposal: dict | None
    applied: list[str] = field(default_factory=list)
    apply_error: str | None = None
    recovered: bool = False
    verification: dict | None = None
    seconds: float = 0.0
    error: str | None = None


@dataclass
class Episode:
    incident: str
    config: str
    trace_dir: str
    alert: dict
    attempts: list[Attempt] = field(default_factory=list)
    grade: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)


def alert_text(incident_day, run) -> str:
    return (f"PAGE: Dagster job `nightly` FAILED for partition {incident_day} (run_id {run.run_id[:8]}, failed step "
            f"`{run.failed_step}`). Investigate, find the root cause, and decide the remediation.")  # fmt: skip


def feedback_text(prev: Attempt, alert: str) -> str:
    v = prev.verification or {}
    return (f"{alert}\n\nA previous attempt already investigated this incident. Its diagnosis was:\n"
            f"{json.dumps(prev.diagnosis, indent=1)[:4000]}\n\nIts proposed remediation "
            f"{json.dumps(prev.proposal['actions'] if prev.proposal else None)} was applied in the sandbox and "
            f"VERIFICATION FAILED: {prev.apply_error or ''} {json.dumps(v)[:2500]}\n"
            "The sandbox was rolled back to its state before that remediation (the failing run is unchanged). "
            "Re-examine the evidence: the root cause may be different from what was concluded. Investigate and "
            "decide again.")  # fmt: skip


def last_proposal(trace: Trace, since: float) -> dict | None:
    props = [e for e in trace.events() if e.get("type") == "proposal" and e["ts"] >= since]
    return props[-1] if props else None


def run_episode(paths: SandboxPaths, incident: Incident, cfg: AgentConfig, out_root: Path = RUNS_DIR) -> Episode:
    day = INCIDENT_DAY
    ref = reference(paths, day)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    trace_dir = out_root / cfg.name / f"{incident.id}-{stamp}"
    trace = Trace(trace_dir)
    cfg.save(trace_dir)

    restore(paths, BASELINE)
    incident.inject(paths, day)
    run = run_pipeline(paths, day)
    if run.run_id == "none" or run.success:
        # The pipeline process itself crashed (or the incident didn't break anything): there is nothing valid to
        # page about, and grading the agent on it would record a failure that isn't the agent's.
        shutil.rmtree(trace_dir, ignore_errors=True)
        raise InfrastructureError(f"{incident.id}: incident run did not produce a valid failure: {run.error}")
    alert = {"partition": str(day), "run_id": run.run_id, "failed_step": run.failed_step, "success": run.success}
    trace.append({"type": "episode_start", "config": cfg.name, "alert": alert})
    ep = Episode(incident.id, cfg.name, str(trace_dir.relative_to(REPO_ROOT)), alert)
    page = alert_text(day, run)
    snapshot(paths, "pre_fix")

    prompt = page
    for n in range(1, MAX_ATTEMPTS + 1):
        t0, since = time.monotonic(), time.time()
        trace.append({"type": "attempt_start", "attempt": n})
        att = Attempt(n, None, None)
        try:
            r = run_master(paths, trace, cfg, prompt, n)
            att.diagnosis = r.structured
        except (ClaudeCLIError, TimeoutError, OSError) as e:
            att.error = str(e)[:1000]
        att.proposal = last_proposal(trace, since)
        att.seconds = round(time.monotonic() - t0, 1)
        trace.append({"type": "diagnosis", "attempt": n, "diagnosis": att.diagnosis, "error": att.error,
                      "seconds": att.seconds})  # fmt: skip

        decided_fix = bool(att.diagnosis and att.diagnosis.get("decision") == "fix")
        if att.proposal and decided_fix:
            try:
                for a in att.proposal["actions"]:
                    att.applied.append(apply_action(paths, a["action"], a.get("params") or {}))
            except (RemediationError, TypeError, KeyError) as e:
                att.apply_error = f"invalid action: {e}"
            if att.apply_error is None:
                v = verify(paths, day, ref)
                att.recovered = v.recovered
                att.verification = {
                    "recovered": v.recovered, "run_success": v.run.success, "failed_step": v.run.failed_step,
                    "error": (v.run.error or "")[:800], "failed_checks": v.failed_checks,
                    "tables_differing_from_known_good": v.mismatched_tables,
                    "run_log_errors": [ln for ln in run_log_lines(paths, v.run.run_id) if " ERROR " in ln][:8],
                }  # fmt: skip
            trace.append({"type": "verification", "attempt": n, "applied": att.applied,
                          "apply_error": att.apply_error, **(att.verification or {})})  # fmt: skip
        ep.attempts.append(att)
        if att.recovered or not decided_fix or att.error:
            break
        restore(paths, "pre_fix")  # roll back the failed remediation before trying again
        trace.append({"type": "rollback", "attempt": n})
        prompt = feedback_text(att, page)

    ep.grade = grade(incident, ep)
    ep.usage = usage(trace)
    trace.append({"type": "episode_end", "grade": ep.grade, "usage": ep.usage})
    (trace_dir / "episode.json").write_text(json.dumps(asdict(ep), indent=2, default=str), encoding="utf-8")
    return ep


# ---------------------------------------------------------------- grading


def _norm(x) -> str | None:
    return str(x).strip().lower().split(".")[-1] if x else None


def root_cause_match(truth: dict, diag: dict | None) -> dict:
    rc = (diag or {}).get("root_cause") or {}
    family = rc.get("family") == truth["family"]
    cause = rc.get("cause") == truth["cause"]
    targets = {k: _norm(rc.get(k)) == _norm(truth[k]) for k in ("table", "column", "model") if k in truth}
    return {"family": family, "cause": cause, "target": all(targets.values()), "targets": targets,
            "correct": family and cause and all(targets.values())}  # fmt: skip


def cited_ids_valid(trace_dir: Path, diag: dict | None) -> tuple[int, int]:
    seen = Trace(trace_dir).evidence()
    ids = [e for item in (diag or {}).get("evidence", []) for e in item.get("evidence_ids", [])]
    return sum(e in seen for e in ids), len(ids)


def grade(incident: Incident, ep: Episode) -> dict:
    first = ep.attempts[0] if ep.attempts else None
    final = ep.attempts[-1] if ep.attempts else None
    state_changing = [
        a for a in ep.attempts if a.applied and any(ACTIONS[x["action"]].changes_state for x in a.proposal["actions"])
    ]
    valid, total = cited_ids_valid(REPO_ROOT / ep.trace_dir, first.diagnosis if first else None)
    expected = "escalate" if incident.outcome == "escalate" else "fix"
    return {
        "expected_decision": expected,
        "first": {
            "root_cause": root_cause_match(incident.root_cause, first.diagnosis if first else None),
            "decision": (first.diagnosis or {}).get("decision") if first else None,
            "recovered": bool(first and first.recovered),
            "confidence": (first.diagnosis or {}).get("confidence") if first else None,
            "seconds": first.seconds if first else None,
        },
        "final": {
            "root_cause": root_cause_match(incident.root_cause, final.diagnosis if final else None),
            "decision": (final.diagnosis or {}).get("decision") if final else None,
            "recovered": bool(final and final.recovered),
        },
        "attempts": len(ep.attempts),
        "correct_decision": (final.diagnosis or {}).get("decision") == expected if final else False,
        # A state-changing fix that wasn't verified as working, or any state change where escalation was right.
        "false_remediations": sum(1 for a in state_changing if not a.recovered or incident.outcome == "escalate"),
        "cited_ids_valid": valid,
        "cited_ids_total": total,
        "errors": [a.error for a in ep.attempts if a.error],
    }


def usage(trace: Trace) -> dict:
    calls = [e for e in trace.events() if e.get("type") == "llm_call"]
    tools = [e for e in trace.events() if e.get("type") == "tool_call"]
    out = {"llm_calls": len(calls), "tool_calls": len(tools), "by_role": {}}
    for role in ("master", "worker"):
        cs = [c for c in calls if c["role"] == role]
        out["by_role"][role] = {
            "calls": len(cs),
            "input_tokens": sum(c["input_tokens"] + c["cache_read_tokens"] + c["cache_write_tokens"] for c in cs),
            "output_tokens": sum(c["output_tokens"] for c in cs),
            "api_equiv_usd": round(sum(c["api_equiv_usd"] for c in cs), 4),
        }
    out["api_equiv_usd"] = round(sum(r["api_equiv_usd"] for r in out["by_role"].values()), 4)
    return out


class UsageLimitReached(RuntimeError):
    pass


_LIMIT_MARKERS = (
    "session limit", "usage limit", "rate limit", "limit reached", "hit your", "resets ", "429", "overloaded",
    "failed to authenticate",
)  # fmt: skip
# The CLI process died or produced no result: an infrastructure failure, not an agent failure. (An agent that runs
# out of turns returns a proper result event and *is* graded.)
_INFRA_MARKERS = ("claude exited", "no result event", "timed out", "timeout")


def run_ids(ids: list[str], cfg: AgentConfig, paths: SandboxPaths) -> list[Episode]:
    eps = []
    for iid in ids:
        ep = run_episode(paths, BY_ID[iid], cfg)
        errs = " ".join(ep.grade.get("errors") or []).lower()
        if any(m in errs for m in _LIMIT_MARKERS + _INFRA_MARKERS):
            # Not the agent's fault: discard the episode so it is rerun later, and stop the batch.
            shutil.rmtree(REPO_ROOT / ep.trace_dir, ignore_errors=True)
            restore(paths, BASELINE)
            raise (UsageLimitReached if any(m in errs for m in _LIMIT_MARKERS) else InfrastructureError)(
                f"{iid}: {errs[:300]} (episode discarded; rerun later)"
            )
        g = ep.grade
        print(f"{iid:<4} {cfg.name:<18} rc={'OK ' if g['first']['root_cause']['correct'] else 'BAD'} "
              f"decision={g['final']['decision']} recovered={g['final']['recovered']} attempts={g['attempts']} "
              f"false_rem={g['false_remediations']} ${ep.usage['api_equiv_usd']:.2f} "
              f"t={g['first']['seconds']}s", flush=True)  # fmt: skip
        eps.append(ep)
    restore(paths, BASELINE)
    return eps


__all__ = ["run_episode", "run_ids", "failed_checks"]
