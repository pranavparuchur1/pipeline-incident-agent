"""The pipeline's plain-text application log, the file an on-call engineer would grep.

One line per event:
    2026-10-14T22:40:01.123 ERROR [run=3f2a9c1e partition=2026-10-01 step=ingest] message...
Line numbers are stable ids: once written, a line never moves, so evidence can cite `log:<n>`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


class RunLog:
    def __init__(self, path: Path, run_id: str, partition: str):
        self.path = path
        self.run_id = run_id
        self.partition = partition
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, level: str, step: str, message: str) -> None:
        ts = datetime.now().isoformat(timespec="milliseconds")
        # One event per line: newlines inside a message (stack traces) are flattened.
        flat = " | ".join(part.strip() for part in message.splitlines() if part.strip())
        line = f"{ts} {level.upper():<5} [run={self.run_id[:8]} partition={self.partition} step={step}] {flat}\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)

    def info(self, step: str, message: str) -> None:
        self.write("INFO", step, message)

    def warn(self, step: str, message: str) -> None:
        self.write("WARN", step, message)

    def error(self, step: str, message: str) -> None:
        self.write("ERROR", step, message)
