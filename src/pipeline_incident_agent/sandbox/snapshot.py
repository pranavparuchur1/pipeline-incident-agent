"""Save and restore the whole sandbox, so every incident starts from the same clean world.

A snapshot is a plain directory copy of the sandbox root: warehouse, landing files, Dagster run
store and logs. Nothing may hold the warehouse open while it is copied.
"""

from __future__ import annotations

import hashlib
import shutil

import duckdb

from .config import SandboxPaths


def snapshot(paths: SandboxPaths, name: str) -> str:
    dest = paths.snapshots / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(paths.root, dest)
    return str(dest)


def restore(paths: SandboxPaths, name: str) -> None:
    src = paths.snapshots / name
    if not src.exists():
        raise FileNotFoundError(f"no snapshot named {name!r} in {paths.snapshots}")
    if paths.root.exists():
        shutil.rmtree(paths.root)
    shutil.copytree(src, paths.root)


def list_snapshots(paths: SandboxPaths) -> list[str]:
    return sorted(p.name for p in paths.snapshots.iterdir()) if paths.snapshots.exists() else []


def fingerprint(paths: SandboxPaths, schemas: tuple[str, ...] = ("raw", "staging", "marts")) -> dict[str, str]:
    """Order-independent content hash per table, ignoring load-time lineage columns.

    Two runs that produce the same data produce the same fingerprints, even though their
    run ids and load timestamps differ.
    """
    out: dict[str, str] = {}
    with duckdb.connect(str(paths.warehouse), read_only=True) as con:
        tables = con.execute(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema in (select unnest(?)) order by 1, 2",
            [list(schemas)],
        ).fetchall()
        for schema, table in tables:
            cols = [
                r[0]
                for r in con.execute(
                    "select column_name from information_schema.columns where table_schema=? and table_name=? "
                    "order by ordinal_position",
                    [schema, table],
                ).fetchall()
                if r[0] not in ("_loaded_at", "_run_id")
            ]
            col_list = ", ".join(f'"{c}"' for c in cols)
            n, h = con.execute(f'select count(*), sum(hash(row({col_list}))) from "{schema}"."{table}"').fetchone()
            out[f"{schema}.{table}"] = hashlib.sha1(f"{n}:{h}".encode()).hexdigest()[:12] + f" ({n} rows)"
    return out
