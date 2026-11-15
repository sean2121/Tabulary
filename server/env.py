"""Secrets come from the environment, never from the database or a command line.

load_dotenv() reads ROOT/.env (KEY=VALUE lines, # comments, optional quotes) into os.environ without overriding
variables that are already set. docker compose reads the same file by itself; this is for running outside Docker
and for the MCP server, which is started by an LLM client and not by a shell that sourced .env.

A saved connection stores the NAME of the environment variable that holds its password (password_env). The value
is handed to psql through PGPASSWORD in the child process environment, so it never appears in `ps` output.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$""")


def load_dotenv(path: Path | None = None) -> None:
    p = path or ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


def password_from_env(var: str | None) -> str:
    """The password named by var, or a clear error for the UI / the LLM client."""
    if not var:
        raise RuntimeError("no password variable set for this connection (enter the name of an environment variable, e.g. TABULARY_PW_CRM, and put its value in .env)")
    v = os.environ.get(var)
    if v is None:
        raise RuntimeError(f"environment variable {var} is not set (add it to .env and restart)")
    return v


def split_password(conninfo: str) -> tuple[str, str | None]:
    """Take password=... out of a libpq connection string so it can travel as PGPASSWORD instead."""
    m = re.search(r"\bpassword=('(?:[^'\\]|\\.)*'|\S+)", conninfo)
    if not m:
        return conninfo, None
    pw = m.group(1)
    if pw.startswith("'"):
        pw = pw[1:-1].replace("\\'", "'").replace("\\\\", "\\")
    return (conninfo[: m.start()] + conninfo[m.end():]).strip(), pw


def pg_env(password: str | None, **extra: str) -> dict[str, str]:
    env = {**os.environ, **extra}
    if password is not None:
        env["PGPASSWORD"] = password
    return env


load_dotenv()
