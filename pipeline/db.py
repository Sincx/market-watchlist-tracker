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


_PK_CACHE: dict[str, list[str]] = {}


def _primary_key(client: libsql_client.ClientSync, table: str) -> list[str]:
    """Primary-key columns of `table`, in key order (cached per process)."""
    if table not in _PK_CACHE:
        rs = client.execute(f"PRAGMA table_info({table})")
        info = [dict(zip(rs.columns, row)) for row in rs.rows]
        _PK_CACHE[table] = [r["name"] for r in sorted((r for r in info if r["pk"]), key=lambda r: r["pk"])]
    return _PK_CACHE[table]


def upsert(
    client: libsql_client.ClientSync,
    table: str,
    rows: Sequence[Mapping],
    columns: Iterable[str] | None = None,
    batch_size: int = 200,
    replace: bool = False,
) -> int:
    """Insert `rows` (dicts) into `table`, updating existing rows in place.
    Returns the row count written.

    **Column-preserving by default (changed 2026-10-19).** Each row only
    touches the columns it actually carries as keys (an UPDATE of just those
    columns for an existing row, an INSERT for a new one — see the comment in the
    body for why not ON CONFLICT). A column a row does
    NOT mention is left exactly as it is on an existing row (and defaults to
    NULL on a brand-new one). A key that IS present with value None is
    written as NULL — "absent" and "explicitly NULL" are different things.

    This replaced INSERT OR REPLACE, which deleted the old row and wrote a
    fresh one, so every column a caller didn't mention was silently reset to
    NULL. Latent damage that caused: universe.py never passes `notes`, so
    each universe refresh would have wiped notes typed into the Watchlist;
    and Phase 17 has filings.py filling new raw-statement columns on the
    same (ticker, exchange, as_of_date) fundamentals rows that
    fundamentals_refresh.py rewrites monthly, which would have erased them.

    Behaviour change to know about: a row that omits a key no longer nulls
    that column on an existing row. A caller that really wants a full
    replace (every unmentioned column reset) passes replace=True — nothing
    in the pipeline currently needs that.

    Rows don't need identical key sets; each row's SQL is built from its own
    keys. `columns`, if given, forces that exact column set onto every row
    (a key missing from a row is then written as NULL, as before). A row
    missing any primary-key column raises ValueError rather than writing a
    NULL key. Batched because Turso's HTTP API caps request size; 200
    statements/batch keeps well under that for our widest tables.
    """
    if not rows:
        return 0

    if replace:
        return _insert_or_replace(client, table, rows, columns, batch_size)

    pk = _primary_key(client, table)
    if not pk:
        raise ValueError(f"upsert: table {table!r} has no primary key — pass replace=True to use INSERT OR REPLACE")

    # Two statements per row, run in the same batch (one transaction):
    #   1. UPDATE <table> SET <only the row's own non-key columns> WHERE <pk>
    #      — touches an existing row, does nothing if there isn't one;
    #   2. INSERT INTO <table> (<the row's columns>) SELECT ... WHERE NOT EXISTS
    #      (<pk match>) — creates the row only if it's genuinely new.
    # Not the more obvious INSERT ... ON CONFLICT DO UPDATE: SQLite checks NOT
    # NULL on the proposed row BEFORE it looks for a key conflict, so a partial
    # row (e.g. just {ticker, exchange, cik}) aimed at an existing row whose
    # table has any NOT NULL column the partial row omits fails outright —
    # found 2026-10-25, and it would have broken investor_refresh's
    # existing-universe-row path. With this shape a partial row works on an
    # existing row, while a partial row for a NEW row still fails loudly on the
    # insert (a new row must be complete) rather than being silently skipped.
    shape_cache: dict[tuple[str, ...], tuple[str | None, str, list[str]]] = {}
    statements = []
    for row in rows:
        keys = tuple(columns) if columns is not None else tuple(row.keys())
        missing = [k for k in pk if k not in keys or row.get(k) is None]
        if missing:
            raise ValueError(f"upsert: row for {table!r} is missing primary-key column(s) {missing}: {dict(row)}")
        shape = shape_cache.get(keys)
        if shape is None:
            update_cols = [c for c in keys if c not in pk]
            where = " AND ".join(f"{k} = :{k}" for k in pk)
            update_sql = (f"UPDATE {table} SET " + ", ".join(f"{c} = :{c}" for c in update_cols) + f" WHERE {where}"
                          if update_cols else None)
            insert_sql = (f"INSERT INTO {table} ({', '.join(keys)}) "
                          f"SELECT {', '.join(f':{c}' for c in keys)} "
                          f"WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE {where})")
            shape = (update_sql, insert_sql, update_cols)
            shape_cache[keys] = shape
        update_sql, insert_sql, update_cols = shape
        if update_sql:
            statements.append(libsql_client.Statement(
                update_sql, {c: row.get(c) for c in [*update_cols, *pk]}))
        statements.append(libsql_client.Statement(insert_sql, {c: row.get(c) for c in keys}))

    # batch_size counts ROWS, so it's up to 2x as many statements per request.
    per_request = batch_size * 2
    for i in range(0, len(statements), per_request):
        client.batch(statements[i : i + per_request])
    return len(rows)


def _insert_or_replace(client, table, rows, columns, batch_size) -> int:
    """The pre-2026-10-19 behaviour: INSERT OR REPLACE over the UNION of
    every row's keys (not just rows[0]'s — using only rows[0] was a real bug
    found 2026-09-10 when universe.py's merge() produced rows with different
    key sets). Any column not in that set is reset to NULL on a replaced row.
    """
    if columns is not None:
        cols = list(columns)
    else:
        cols, seen = [], set()
        for row in rows:
            for k in row.keys():
                if k not in seen:
                    seen.add(k)
                    cols.append(k)
    sql = (f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
           f"VALUES ({', '.join(f':{c}' for c in cols)})")
    written = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        client.batch([libsql_client.Statement(sql, {c: row.get(c) for c in cols}) for row in chunk])
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

    upsert() is column-preserving now (see its docstring), so the static
    metadata fields below no longer get clobbered if omitted — but they're
    still passed on every call, so a first registration and a re-run write
    the same full row. `status` is a short
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
