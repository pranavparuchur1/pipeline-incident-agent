"""Export recorded episodes into dashboard/data.json (+ chart images) for the static dashboard.

The dashboard never calls a model or touches the sandbox: it replays what the traces recorded, so
anyone can open it without a Claude login.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..eval.report import SETS, metrics
from ..incidents.catalog import BY_ID, DEV_SET, INCIDENTS
from ..sandbox.config import REPO_ROOT

RUNS = REPO_ROOT / "runs"
OUT = REPO_ROOT / "dashboard"
CONFIG_ORDER = ["opus+sonnet", "opus-solo", "sonnet-only", "opus+sonnet-noimg", "v1-opus-alltools"]
MAX_TEXT = 1800


def _clip(s: str, n: int = MAX_TEXT) -> str:
    return s if len(s) <= n else s[:n] + " …"


def timeline(trace_file: Path, assets: Path, key: str) -> tuple[list[dict], dict[str, dict]]:
    events, evidence = [], {}
    t0 = None
    for ln in trace_file.read_text(encoding="utf-8").splitlines():
        e = json.loads(ln)
        t0 = t0 or e["ts"]
        at = round(e["ts"] - t0, 1)
        t = e["type"]
        if t == "tool_call":
            images = []
            for png in e.get("images") or []:
                src = Path(png)
                if src.exists():
                    name = f"{key}-{src.name}"
                    shutil.copyfile(src, assets / name)
                    images.append(f"assets/{name}")
            for ev in e.get("evidence", []):
                evidence[ev["id"]] = {"kind": ev["kind"], "text": _clip(ev["text"])}
            events.append({"t": at, "kind": "tool", "agent": e.get("agent"), "tool": e["tool"], "args": e["args"],
                           "ok": e["ok"], "summary": _clip(e["summary"], 400), "evidence": [x["id"] for x in e["evidence"]],
                           "images": images, "ms": e.get("ms")})  # fmt: skip
        elif t == "llm_call":
            events.append({"t": at, "kind": "llm", "agent": e["agent"], "role": e["role"], "model": e["model"],
                           "turns": e["num_turns"], "seconds": round(e["duration_ms"] / 1000, 1),
                           "usd": round(e["api_equiv_usd"], 4), "task": e.get("task"),
                           "answer": (e.get("structured") or {}).get("answer") if e["role"] == "worker" else None})  # fmt: skip
        elif t in ("proposal", "verification", "rollback", "attempt_start", "denied", "worker_error"):
            events.append({"t": at, "kind": t, **{k: v for k, v in e.items() if k not in ("ts", "type")}})
    return events, evidence


def export(out: Path = OUT) -> Path:
    assets = out / "assets"
    if assets.exists():
        shutil.rmtree(assets)
    assets.mkdir(parents=True)
    configs = [c for c in CONFIG_ORDER if (RUNS / c).exists()]
    episodes = []
    for cfg in configs:
        for f in sorted((RUNS / cfg).glob("*/episode.json")):
            ep = json.loads(f.read_text(encoding="utf-8"))
            ep["config"] = cfg  # the directory is authoritative (runs may be re-filed under a new name)
            key = f"{cfg}-{f.parent.name}".replace("+", "_")
            events, evidence = timeline(f.parent / "trace.jsonl", assets, key)
            episodes.append({**ep, "key": key, "events": events, "evidence": evidence,
                             "set": "dev" if ep["incident"] in DEV_SET else "held-out"})  # fmt: skip
    data = {
        "incidents": [
            {
                "id": i.id,
                "family": i.family,
                "title": i.title,
                "outcome": i.outcome,
                "root_cause": i.root_cause,
                "set": "dev" if i.id in DEV_SET else "held-out",
            }
            for i in INCIDENTS
        ],  # fmt: skip
        "configs": configs,
        "metrics": {s: [metrics(c, ids) for c in configs if c != "v1-opus-alltools"] for s, ids in SETS.items()},
        "episodes": episodes,
    }
    out.mkdir(parents=True, exist_ok=True)
    f = out / "data.json"
    f.write_text(json.dumps(data, default=str), encoding="utf-8")
    # Also as a script, so index.html works when opened straight from disk (file:// blocks fetch).
    (out / "data.js").write_text("window.PIA_DATA = " + json.dumps(data, default=str) + ";\n", encoding="utf-8")
    return f


__all__ = ["export", "BY_ID"]
