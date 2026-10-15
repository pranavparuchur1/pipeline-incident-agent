"""Aggregate episode results into the evaluation metrics.

Each run of the agent on an incident writes runs/<config>/<incident>-<stamp>/episode.json. For each
config and incident set we take the latest episode per incident and compute:

  root-cause accuracy     family + cause + target (table/column/model) all match ground truth (1st attempt)
  decision accuracy       fix vs escalate matches what the incident allows
  recovery rate           verified recovery, on incidents that can be fixed (1st attempt and with 1 retry)
  correct escalation      escalated, with no state-changing fix, on incidents with no safe fix
  false remediation rate  applied state-changing fixes that were NOT verified as working (or any on escalate-only)
  time to diagnosis       wall-clock seconds of the first master attempt (includes worker sessions)
  cost per incident       API-equivalent dollars (an estimate: these runs use a subscription, not the API)

Rates carry Wilson 95% intervals; with ~20 incidents per set, differences under ~20 points are noise.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

from ..incidents.catalog import BY_ID, DEV_SET, HELD_OUT
from ..sandbox.config import REPO_ROOT

RUNS = REPO_ROOT / "runs"
SETS = {"dev": DEV_SET, "held-out": HELD_OUT}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def latest_episodes(config: str, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in sorted((RUNS / config).glob("*/episode.json")):
        ep = json.loads(f.read_text(encoding="utf-8"))
        if ep["incident"] in ids:
            out[ep["incident"]] = ep  # sorted by stamp, so the last one wins
    return out


def rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": round(k / n, 3) if n else None, "ci95": [round(lo, 2), round(hi, 2)]}


def metrics(config: str, ids: list[str]) -> dict:
    eps = latest_episodes(config, ids)
    if not eps:
        return {"config": config, "n": 0}
    g = {i: e["grade"] for i, e in eps.items()}
    fixable = [i for i in eps if BY_ID[i].outcome == "recover"]
    escal = [i for i in eps if BY_ID[i].outcome == "escalate"]
    applied = sum(len([a for a in e["attempts"] if a["applied"]]) for e in eps.values())
    secs = [g[i]["first"]["seconds"] for i in eps if g[i]["first"]["seconds"] is not None]
    cost = [e["usage"]["api_equiv_usd"] for e in eps.values()]
    worker_cost = [e["usage"]["by_role"]["worker"]["api_equiv_usd"] for e in eps.values()]
    conf_ok = [g[i]["first"]["confidence"] for i in eps if g[i]["first"]["root_cause"]["correct"]]
    conf_bad = [g[i]["first"]["confidence"] for i in eps if not g[i]["first"]["root_cause"]["correct"]]
    cited = sum(g[i]["cited_ids_valid"] for i in eps), sum(g[i]["cited_ids_total"] for i in eps)
    return {
        "config": config,
        "n": len(eps),
        "missing": [i for i in ids if i not in eps],
        "root_cause_accuracy": rate(sum(g[i]["first"]["root_cause"]["correct"] for i in eps), len(eps)),
        "root_cause_family_accuracy": rate(sum(g[i]["first"]["root_cause"]["family"] for i in eps), len(eps)),
        "decision_accuracy": rate(sum(g[i]["correct_decision"] for i in eps), len(eps)),
        "recovery_first_attempt": rate(sum(g[i]["first"]["recovered"] for i in fixable), len(fixable)),
        "recovery_with_retry": rate(sum(g[i]["final"]["recovered"] for i in fixable), len(fixable)),
        "correct_escalation": rate(
            sum(g[i]["final"]["decision"] == "escalate" and g[i]["false_remediations"] == 0 for i in escal), len(escal)
        ),
        "false_remediation": {
            "count": sum(g[i]["false_remediations"] for i in eps),
            "applied_fixes": applied,
            **rate(sum(g[i]["false_remediations"] for i in eps), applied),
        },
        "time_to_diagnosis_s": {
            "median": round(statistics.median(secs), 1) if secs else None,
            "p90": round(sorted(secs)[int(0.9 * (len(secs) - 1))], 1) if secs else None,
        },
        "cost_per_incident_usd": {
            "mean": round(statistics.mean(cost), 3),
            "median": round(statistics.median(cost), 3),
            "worker_share": round(sum(worker_cost) / sum(cost), 2) if sum(cost) else 0.0,
        },
        "delegations_per_incident": round(
            statistics.mean(e["usage"]["by_role"]["worker"]["calls"] for e in eps.values()), 2
        ),
        "tool_calls_per_incident": round(statistics.mean(e["usage"]["tool_calls"] for e in eps.values()), 1),
        "citations_valid": {"valid": cited[0], "total": cited[1]},
        "confidence_when_correct": round(statistics.mean(conf_ok), 2) if conf_ok else None,
        "confidence_when_wrong": round(statistics.mean(conf_bad), 2) if conf_bad else None,
        "errors": {i: g[i]["errors"] for i in eps if g[i]["errors"]},
        "per_incident": {
            i: {
                "root_cause_ok": g[i]["first"]["root_cause"]["correct"],
                "decision": g[i]["final"]["decision"],
                "recovered": g[i]["final"]["recovered"],
                "attempts": g[i]["attempts"],
                "false_remediations": g[i]["false_remediations"],
                "seconds": g[i]["first"]["seconds"],
                "usd": eps[i]["usage"]["api_equiv_usd"],
            }
            for i in sorted(eps)
        },
    }


def _pct(r: dict) -> str:
    if not r.get("n"):
        return "–"
    return f"{r['k']}/{r['n']} ({r['rate']:.0%}, CI {r['ci95'][0]:.0%}–{r['ci95'][1]:.0%})"


def markdown(results: list[dict]) -> str:
    rows = [
        ("Root-cause accuracy (exact)", lambda m: _pct(m["root_cause_accuracy"])),
        ("Root-cause family accuracy", lambda m: _pct(m["root_cause_family_accuracy"])),
        ("Decision accuracy (fix vs escalate)", lambda m: _pct(m["decision_accuracy"])),
        ("Recovery, first attempt", lambda m: _pct(m["recovery_first_attempt"])),
        ("Recovery, with 1 retry", lambda m: _pct(m["recovery_with_retry"])),
        ("Correct escalation", lambda m: _pct(m["correct_escalation"])),
        ("False remediations / applied fixes", lambda m: f"{m['false_remediation']['count']}/{m['false_remediation']['applied_fixes']}"),
        ("Time to diagnosis, median (p90)", lambda m: f"{m['time_to_diagnosis_s']['median']} s ({m['time_to_diagnosis_s']['p90']} s)"),
        ("Cost per incident, mean (API-equiv.)", lambda m: f"${m['cost_per_incident_usd']['mean']:.2f} (workers {m['cost_per_incident_usd']['worker_share']:.0%})"),
        ("Delegations / tool calls per incident", lambda m: f"{m['delegations_per_incident']} / {m['tool_calls_per_incident']}"),
        ("Cited evidence ids that exist", lambda m: f"{m['citations_valid']['valid']}/{m['citations_valid']['total']}"),
        ("Mean confidence: correct / wrong", lambda m: f"{m['confidence_when_correct']} / {m['confidence_when_wrong']}"),
    ]  # fmt: skip
    present = [m for m in results if m.get("n")]
    head = "| Metric | " + " | ".join(f"{m['config']} (n={m['n']})" for m in present) + " |"
    sep = "|---" * (len(present) + 1) + "|"
    body = "\n".join(f"| {name} | " + " | ".join(fn(m) for m in present) + " |" for name, fn in rows)
    return "\n".join([head, sep, body])


def report(configs: list[str], set_name: str) -> tuple[list[dict], str]:
    ids = SETS[set_name]
    results = [metrics(c, ids) for c in configs]
    return results, markdown(results)


def write(configs: list[str], set_name: str, out_dir: Path) -> Path:
    results, md = report(configs, set_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"metrics_{set_name}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    f = out_dir / f"metrics_{set_name}.md"
    f.write_text(md + "\n", encoding="utf-8")
    return f
