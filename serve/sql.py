"""Read-only SQL execution for the serve layer.

Defence in depth: the statement is checked before it is sent (one SELECT/WITH statement, no write keywords), and it
runs inside a READ ONLY transaction with a statement timeout and a row cap. Results come back from psql as CSV.
"""
from __future__ import annotations

import csv
import io
import re
import subprocess

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|vacuum|analyze|call|do|lock|"
    r"refresh|cluster|reindex|comment|listen|notify|set|reset|show|begin|commit|rollback|savepoint|prepare|execute|"
    r"deallocate|discard|load|security|pg_sleep|pg_read_file|pg_read_binary_file|pg_ls_dir|lo_import|lo_export|dblink)\b",
    re.IGNORECASE,
)


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def check(sql: str) -> str:
    """Return the cleaned statement or raise ValueError with a message meant for the calling LLM."""
    s = _strip_comments(sql).strip().rstrip(";").strip()
    if not s:
        raise ValueError("empty statement")
    if ";" in s:
        raise ValueError("one statement at a time (';' found inside the statement)")
    if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
        raise ValueError("only SELECT (or WITH ... SELECT) statements are allowed")
    # ignore keywords inside string literals and quoted identifiers
    bare = re.sub(r"'(?:[^']|'')*'", "''", s)
    bare = re.sub(r'"[^"]*"', '""', bare)
    m = FORBIDDEN.search(bare)
    if m:
        raise ValueError(f"forbidden keyword in statement: {m.group(0)}")
    return s


def run(conninfo: str, sql: str, schema: str = "public", limit: int = 100, timeout_s: int = 15, env: dict | None = None) -> dict:
    """Execute a checked SELECT and return {"columns": [...], "rows": [[...]], "truncated": bool}.
    conninfo carries no password; env (with PGPASSWORD) is the child-process environment."""
    s = check(sql)
    limit = max(1, min(int(limit), 1000))
    wrapped = f"SELECT * FROM ({s}) AS tabulary_q LIMIT {limit + 1}"
    cmd = ["psql", conninfo, "-q", "--csv", "--single-transaction", "-v", "ON_ERROR_STOP=1",
           "-c", "SET TRANSACTION READ ONLY",
           "-c", f"SET LOCAL statement_timeout = '{int(timeout_s) * 1000}ms'",
           "-c", f'SET LOCAL search_path TO "{schema}", public',
           "-c", wrapped]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 10, env=env)
    if r.returncode != 0:
        raise ValueError(r.stderr.strip() or "query failed")
    rows = list(csv.reader(io.StringIO(r.stdout)))
    cols, data = (rows[0], rows[1:]) if rows else ([], [])
    truncated = len(data) > limit
    return {"columns": cols, "rows": data[:limit], "truncated": truncated}


def to_markdown(res: dict, limit: int) -> str:
    cols, rows = res["columns"], res["rows"]
    if not cols:
        return "(no columns)"
    if not rows:
        return "| " + " | ".join(cols) + " |\n" + "(0 rows)"
    L = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        L.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in r) + " |")
    L.append(f"({len(rows)} rows" + (f", truncated at limit {limit}; add a WHERE or aggregate instead of raising the limit" if res["truncated"] else "") + ")")
    return "\n".join(L)
