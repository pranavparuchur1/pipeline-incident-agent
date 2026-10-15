"""Inject incidents into a fresh copy of the baseline, and prove the catalog's claims.

For each incident the gate checks three things, each from a fresh restore of `baseline`:

  breaks     inject -> run: the run fails at the expected step, and the expected evidence
             appears in that run's log lines / error / failed checks
  true fix   inject -> apply each true fix -> verify: recovered (or, for an "escalate"
             incident, still failing and untouched: there is no automated fix to credit)
  decoys     inject -> apply each decoy -> verify: NOT recovered
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import date

from ..sandbox.config import INCIDENT_DAY, SandboxPaths
from ..sandbox.pipeline import run_pipeline
from ..sandbox.remediation import apply_action
from ..sandbox.snapshot import fingerprint, restore
from ..sandbox.verify import Verification, failed_checks, load_reference, run_log_lines, save_reference, verify
from .catalog import BY_ID, Incident, resolve_fix

BASELINE = "baseline"


def reference(paths: SandboxPaths, day: date = INCIDENT_DAY) -> dict[str, str]:
    """Fingerprint of a clean run of `day` from the baseline (computed once, then cached)."""
    name = f"{BASELINE}-{day.isoformat()}"
    if (ref := load_reference(paths, name)) is not None:
        return ref
    restore(paths, BASELINE)
    run = run_pipeline(paths, day)
    if not run.success:
        raise RuntimeError(f"clean reference run failed: {run.error}")
    ref = fingerprint(paths)
    save_reference(paths, name, ref)
    return ref


def inject(paths: SandboxPaths, incident: Incident, day: date = INCIDENT_DAY) -> None:
    restore(paths, BASELINE)
    incident.inject(paths, day)


def apply_fix(paths: SandboxPaths, fix, day: date) -> list[str]:
    return [apply_action(paths, name, params) for name, params in resolve_fix(fix, day)]


@dataclass
class Trial:
    kind: str  # break | true_fix | decoy
    fix: list | None
    passed: bool
    detail: str
    seconds: float


@dataclass
class IncidentReport:
    id: str
    title: str
    trials: list[Trial] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(t.passed for t in self.trials)


def _describe(v: Verification) -> str:
    if v.recovered:
        return "recovered"
    parts = [f"run {'ok' if v.run.success else 'failed at ' + str(v.run.failed_step)}"]
    if v.failed_checks:
        parts.append(f"failed checks {v.failed_checks[:4]}")
    if v.mismatched_tables:
        parts.append(f"differs from reference: {v.mismatched_tables[:4]}")
    return "; ".join(parts)


def check_incident(paths: SandboxPaths, incident: Incident, ref: dict[str, str], day: date = INCIDENT_DAY):
    rep = IncidentReport(incident.id, incident.title)

    t0 = time.monotonic()
    inject(paths, incident, day)
    run = run_pipeline(paths, day)
    haystack = "\n".join([run.error or "", *run_log_lines(paths, run.run_id), *failed_checks(paths, run.run_id)])
    missing = [e for e in incident.expected_evidence if e not in haystack]
    ok = (not run.success) and run.failed_step == incident.expected_failed_step and not missing
    detail = f"failed_step={run.failed_step} (expected {incident.expected_failed_step})"
    if missing:
        detail += f"; evidence not found: {missing}"
    rep.trials.append(Trial("break", None, ok, detail, time.monotonic() - t0))

    for kind, fixes in (("true_fix", incident.true_fixes), ("decoy", incident.decoy_fixes)):
        for fix in fixes:
            t0 = time.monotonic()
            inject(paths, incident, day)
            try:
                applied = apply_fix(paths, fix, day)
            except Exception as e:  # an invalid fix is a harness bug, not a pass
                rep.trials.append(Trial(kind, fix, False, f"action error: {e}", time.monotonic() - t0))
                continue
            v = verify(paths, day, ref)
            if kind == "decoy":
                ok = not v.recovered
            elif incident.outcome == "escalate":
                ok = not v.recovered  # escalation changes nothing; the pipeline must still be broken
            else:
                ok = v.recovered
            detail = f"{_describe(v)} | {'; '.join(applied)}"
            rep.trials.append(Trial(kind, fix, ok, detail, time.monotonic() - t0))
    return rep


def run_gate(paths: SandboxPaths, ids: list[str] | None = None, out_json=None) -> list[IncidentReport]:
    ref = reference(paths)
    reports = []
    for iid in ids or list(BY_ID):
        rep = check_incident(paths, BY_ID[iid], ref)
        reports.append(rep)
        status = "PASS" if rep.passed else "FAIL"
        print(f"[{status}] {rep.id} {rep.title}", flush=True)
        for t in rep.trials:
            mark = "ok " if t.passed else "BAD"
            fix = "" if t.fix is None else " " + json.dumps(t.fix)
            print(f"    {mark} {t.kind:<8}{fix[:110]}  -> {t.detail[:200]}  ({t.seconds:.0f}s)", flush=True)
    restore(paths, BASELINE)
    if out_json:
        out_json.write_text(json.dumps([asdict(r) | {"passed": r.passed} for r in reports], indent=2))
    return reports
