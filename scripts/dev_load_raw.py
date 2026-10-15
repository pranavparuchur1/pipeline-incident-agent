"""Dev helper: generate N days of landing files into a scratch sandbox and load them into raw.

Usage: uv run python scripts/dev_load_raw.py <sandbox_dir> [days]
"""

import sys
from datetime import timedelta
from pathlib import Path

from pipeline_incident_agent.sandbox.config import HISTORY_START, SandboxPaths
from pipeline_incident_agent.sandbox.generator import generate_day, generate_products
from pipeline_incident_agent.sandbox.ingest import load_partition

paths = SandboxPaths(Path(sys.argv[1]).resolve())
days = int(sys.argv[2]) if len(sys.argv) > 2 else 3
paths.ensure()
generate_products(paths)
for i in range(days):
    d = HISTORY_START + timedelta(days=i)
    counts = generate_day(paths, d, HISTORY_START)
    loaded = load_partition(paths, d, run_id=f"dev-{d}")
    print(d, counts, [(r.table, r.rows) for r in loaded])
print("warehouse:", paths.warehouse)
