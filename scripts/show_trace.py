"""Print a readable timeline of one episode's trace.

Usage: uv run python scripts/show_trace.py [runs/<config>/<incident-stamp>]   (default: the newest)
"""

import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "runs"
d = Path(sys.argv[1]) if len(sys.argv) > 1 else max(root.glob("*/*/"), key=lambda p: p.stat().st_mtime)
print(d)
t0 = None
for ln in (d / "trace.jsonl").read_text(encoding="utf-8").splitlines():
    e = json.loads(ln)
    t0 = t0 or e["ts"]
    at = f"{e['ts'] - t0:6.0f}s"
    t = e["type"]
    if t == "tool_call":
        print(f"{at} {e.get('agent', '?'):<26} {e['tool']:<22} {json.dumps(e['args'])[:90]}")
        print(f"{'':>8} {'':<26} -> {e['summary'][:150]}")
    elif t == "llm_call":
        print(f"{at} {e['agent']:<26} LLM {e['model']} turns={e['num_turns']} out={e['output_tokens']} "
              f"{e['duration_ms'] / 1000:.0f}s ${e['api_equiv_usd']:.3f}")
    elif t == "proposal":
        print(f"{at} PROPOSAL {json.dumps(e['actions'])} cites {e['evidence_ids']}")
    elif t == "diagnosis":
        rc = (e.get("diagnosis") or {}).get("root_cause", {})
        print(f"{at} DIAGNOSIS attempt {e['attempt']}: {rc.get('family')}/{rc.get('cause')} "
              f"table={rc.get('table')} column={rc.get('column')} model={rc.get('model')} "
              f"decision={(e.get('diagnosis') or {}).get('decision')} conf={(e.get('diagnosis') or {}).get('confidence')}"
              + (f" ERROR {e['error'][:200]}" if e.get("error") else ""))
    elif t == "verification":
        print(f"{at} VERIFY recovered={e.get('recovered')} applied={e.get('applied')} err={e.get('apply_error')} "
              f"failed_checks={e.get('failed_checks')} differs={e.get('tables_differing_from_known_good')}")
    elif t == "episode_end":
        print(f"{at} END grade={json.dumps(e['grade'])[:400]}\n{'':>8} usage={json.dumps(e['usage'])}")
    else:
        print(f"{at} {t} {json.dumps({k: v for k, v in e.items() if k not in ('ts', 'type')})[:160]}")
