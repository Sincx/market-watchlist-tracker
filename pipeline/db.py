"""Turso (hosted libSQL) client wrapper — the single point of DB access for
the pipeline. Every module that reads or writes Turso goes through this one.
"""
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import os
from dotenv import load_dotenv
import libsql_client

_ENV_LOADED = False


def _ensure_env() -> None:
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv(Path(__file__).parent / ".env")
        _ENV_LOADED = True


def get_client() -> libsql_client.ClientSync:
    """Create a new Turso client. Caller is responsible for closing it
    (use as a context manager via `with db.get_client() as client:`).
    """
    _ensure_env()
    url = os.environ["TURSO_DATABASE_URL"].replace("libsql://", "https://")
    token = os.environ["TURSO_AUTH_TOKEN"]
    return libsql_client.create_client_sync(url=url, auth_token=token)


def upsert(
    client: libsql_client.ClientSync,
    table: str,
    rows: Sequence[Mapping],
    columns: Iterable[str] | None = None,
    batch_size: int = 200,
) -> int:
    """INSERT OR REPLACE `rows` (dicts) into `table` in batches.

    Rows don't need identical key sets — a key missing from a given row is
    written as NULL. Returns the row count written. Batched because Turso's
    HTTP API caps request size; 200 rows/batch keeps well under that for
    our widest tables (prices, fundamentals).

    `columns`, if given, must be the full set to write — otherwise it's the
    UNION of every row's keys (not just rows[0]'s). Using only rows[0] was a
    real bug found 2026-09-10: universe.py's merge() produces rows with
    different key sets depending on source, and whichever key rows[0]
    happened to lack (sa_prefix, for an S&P 500 row) got silently dropped
    for every row, not just the ones actually missing it.
    """
    if not rows:
        return 0
    if columns is not None:
        cols = list(columns)
    else:
        cols = []
        seen = set()
        for row in rows:
            for k in row.keys():
                if k not in seen:
                    seen.add(k)
                    cols.append(k)
    col_list = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"

    written = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        statements = [
            libsql_client.Statement(sql, {c: row.get(c) for c in cols})
            for row in chunk
        ]
        client.batch(statements)
        written += len(chunk)
    return written


def query(client: libsql_client.ClientSync, sql: str, args=None) -> list[dict]:
    """Run a SELECT and return rows as plain dicts."""
    rs = client.execute(sql, args)
    return [dict(zip(rs.columns, row)) for row in rs.rows]


def record_task_run(
    client: libsql_client.ClientSync,
    task_id: str,
    status: str,
    kind: str | None = None,
    schedule_cron: str | None = None,
    description: str | None = None,
    entry_point: str | None = None,
) -> None:
    """Write task_registry's one row for `task_id`. Phase 3 spec (2026-09-19)
    §3.3 — nothing wrote to this table before, so a scheduled task silently
    not firing (confirmed live 2026-09-19: refresh-technicals skipped 3
    straight days with zero visibility anywhere) went undetected for days.

    Full INSERT OR REPLACE (see upsert()'s docstring) — always pass the
    static metadata fields too, every call, not just on first registration,
    or they get clobbered to NULL on the next run. `status` is a short
    string ('success: ...' / 'error: ...') truncated to 500 chars — the
    schema has one last_run_status TEXT column, no separate error-detail
    field, deliberately (Phase 3 scoped this as a pipeline-code fix, not a
    schema change).
    """
    upsert(client, "task_registry", [{
        "task_id": task_id, "kind": kind, "schedule_cron": schedule_cron,
        "description": description, "entry_point": entry_point,
        "last_run_at": datetime.now(timezone.utc).isoformat(),
        "last_run_status": status[:500],
    }])
