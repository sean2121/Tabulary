"""Saved database connections (a Sequel Pro-style favorites list) and a small schema / table browser.

A connection stores the name of the environment variable holding its password (password_env), not the password.
Values live in .env (see server/env.py); psql receives them as PGPASSWORD, never on the command line.
Only PostgreSQL is supported for now; extraction (scan/extract_pg.py) and Ontop's JDBC driver are Postgres-specific.
"""
from __future__ import annotations

import csv
import io
import os
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from server import env as E

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("TABULARY_DATA_DIR", str(ROOT / "data"))) / "tabulary.db"
templates = Jinja2Templates(directory=str(ROOT / "server" / "templates"))
router = APIRouter()

KINDS = [("postgresql", "PostgreSQL", True), ("mysql", "MySQL (not yet)", False), ("sqlserver", "SQL Server (not yet)", False), ("oracle", "Oracle (not yet)", False)]
_DEMO_HOST, _DEMO_PORT = os.environ.get("DEMO_DB_HOST", "localhost"), int(os.environ.get("DEMO_DB_PORT", "5433"))
SEED = [  # demo databases from docker-compose.yml, registered on first start
    {"name": "Insurance demo", "host": _DEMO_HOST, "port": _DEMO_PORT, "dbname": "tabulary", "user": "tabulary", "password_env": "DEMO_DB_PASSWORD"},
    {"name": "University demo (Ontop tutorial)", "host": _DEMO_HOST, "port": _DEMO_PORT, "dbname": "uni", "user": "tabulary", "password_env": "DEMO_DB_PASSWORD"},
    {"name": "Retail demo (abbreviated names, no foreign keys)", "host": _DEMO_HOST, "port": _DEMO_PORT, "dbname": "retail", "user": "tabulary", "password_env": "DEMO_DB_PASSWORD"},
]
os.environ.setdefault("DEMO_DB_PASSWORD", "tabulary")  # the demo Postgres default from docker-compose.yml


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


with db() as _c:
    _c.execute("""CREATE TABLE IF NOT EXISTS connections (
        id TEXT PRIMARY KEY, name TEXT, kind TEXT, host TEXT, port INTEGER, dbname TEXT, user TEXT, password TEXT, created_at TEXT)""")
    if "password_env" not in [r[1] for r in _c.execute("PRAGMA table_info(connections)")]:
        # migration from plaintext passwords: the column stays (SQLite), its contents are wiped; demo rows point at DEMO_DB_PASSWORD
        _c.execute("ALTER TABLE connections ADD COLUMN password_env TEXT")
        _c.execute("UPDATE connections SET password_env='DEMO_DB_PASSWORD' WHERE user='tabulary' AND dbname IN ('tabulary','uni')")
        _c.execute("UPDATE connections SET password=NULL")
    if not _c.execute("SELECT 1 FROM connections LIMIT 1").fetchone():
        for s in SEED:
            _c.execute("INSERT INTO connections (id,name,kind,host,port,dbname,user,password_env,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), s["name"], "postgresql", s["host"], s["port"], s["dbname"], s["user"], s["password_env"], now()))


# ──────────────────────────────── helpers ────────────────────────────────


def _q(v) -> str:
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def conninfo(c: dict) -> str:
    """libpq connection string for a saved connection (values quoted). No password: see pg_env()."""
    return " ".join(f"{k}={_q(c[k])}" for k in ("host", "port", "dbname", "user") if c.get(k) not in (None, ""))


def pg_env(c: dict, **extra: str) -> dict[str, str]:
    """Child-process environment carrying the connection's password as PGPASSWORD."""
    return E.pg_env(E.password_from_env(c.get("password_env")), **extra)


def get(conn_id: str) -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM connections WHERE id=?", (conn_id,)).fetchone()
    if not r:
        raise HTTPException(404, "connection not found")
    return dict(r)


def list_all() -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,name,kind,host,port,dbname,user,password_env FROM connections ORDER BY name")]


def _psql(c: dict, sql: str) -> list[dict]:
    r = subprocess.run(["psql", conninfo(c), "--csv", "-v", "ON_ERROR_STOP=1", "-c", sql], capture_output=True, text=True, timeout=15,
                       env=pg_env(c, PGCONNECT_TIMEOUT="5"))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "connection failed").strip().splitlines()[-1])
    return list(csv.DictReader(io.StringIO(r.stdout)))


def schemas(c: dict) -> list[dict]:
    return _psql(c, """SELECT table_schema AS name, COUNT(*) AS tables FROM information_schema.tables
        WHERE table_schema NOT IN ('pg_catalog','information_schema') AND table_schema NOT LIKE 'pg_toast%'
        GROUP BY 1 ORDER BY 1""")


def tables(c: dict, schema: str) -> list[dict]:
    lit = "'" + schema.replace("'", "''") + "'"
    return _psql(c, f"""SELECT cl.relname AS name,
            CASE cl.relkind WHEN 'v' THEN 'view' ELSE 'table' END AS kind,
            (SELECT COUNT(*) FROM information_schema.columns ic WHERE ic.table_schema = n.nspname AND ic.table_name = cl.relname) AS columns,
            CASE WHEN cl.reltuples < 0 THEN NULL ELSE cl.reltuples::bigint END AS est_rows,
            (SELECT COUNT(*) FROM pg_constraint k WHERE k.conrelid = cl.oid AND k.contype = 'f') AS fks,
            EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid = cl.oid AND k.contype = 'p') AS has_pk
        FROM pg_class cl JOIN pg_namespace n ON n.oid = cl.relnamespace
        WHERE n.nspname = {lit} AND cl.relkind IN ('r','p','v') ORDER BY 1""")


def home_ctx(conn_id: str | None = None, schema: str | None = None) -> dict:
    """Context for the connection area of the home page."""
    conns = list_all()
    sel = next((x for x in conns if x["id"] == conn_id), conns[0] if conns else None)
    ctx = {"connections": conns, "conn": None, "kinds": KINDS, "schemas": [], "schema": None, "tables": [], "conn_error": None}
    if sel:
        ctx.update(_detail_ctx(get(sel["id"]), schema))
    return ctx


def _detail_ctx(c: dict, schema: str | None = None) -> dict:
    ctx = {"conn": {k: v for k, v in c.items() if k != "password"}, "kinds": KINDS, "schemas": [], "schema": None, "tables": [], "conn_error": None,
           "pw_set": bool(c.get("password_env")) and c["password_env"] in os.environ}
    try:
        ctx["schemas"] = schemas(c)
    except Exception as e:  # noqa: BLE001
        ctx["conn_error"] = str(e)
        return ctx
    names = [s["name"] for s in ctx["schemas"]]
    ctx["schema"] = schema if schema in names else ("public" if "public" in names else (names[0] if names else None))
    if ctx["schema"]:
        ctx["tables"] = tables(c, ctx["schema"])
    return ctx


# ──────────────────────────────── UI ────────────────────────────────


@router.get("/ui/connections/new", response_class=HTMLResponse)
def ui_new(request: Request):
    return templates.TemplateResponse(request, "conn_detail.html", {"conn": None, "kinds": KINDS, "connections": list_all(), "oob": True})


@router.get("/ui/connections/{conn_id}", response_class=HTMLResponse)
def ui_detail(request: Request, conn_id: str, schema: str | None = None):
    return templates.TemplateResponse(request, "conn_detail.html", {**_detail_ctx(get(conn_id), schema), "connections": list_all(), "oob": True})


@router.get("/ui/connections/{conn_id}/tables", response_class=HTMLResponse)
def ui_tables(request: Request, conn_id: str, schema_name: str):
    c = get(conn_id)
    return templates.TemplateResponse(request, "conn_tables.html", {"conn": c, "schema": schema_name, "tables": tables(c, schema_name)})


def _form_conn(conn_id: str, name: str, kind: str, host: str, port: str, dbname: str, user: str, password_env: str) -> dict:
    return {"id": conn_id or None, "name": name.strip() or f"{dbname}@{host}", "kind": kind, "host": host.strip(), "port": int(port or 5432),
            "dbname": dbname.strip(), "user": user.strip(), "password_env": password_env.strip()}


@router.post("/ui/connections/test", response_class=HTMLResponse)
def ui_test(conn_id: str = Form(""), name: str = Form(""), kind: str = Form("postgresql"), host: str = Form(""), port: str = Form("5432"),
            dbname: str = Form(""), user: str = Form(""), password_env: str = Form("")):
    if kind != "postgresql":
        return HTMLResponse('<span id="testresult" class="bad">Only PostgreSQL is supported for now.</span>')
    c = _form_conn(conn_id, name, kind, host, port, dbname, user, password_env)
    try:
        ss = schemas(c)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f'<span id="testresult" class="bad">✕ {_esc(str(e))}</span>')
    n = sum(int(s["tables"]) for s in ss)
    return HTMLResponse(f'<span id="testresult" class="good">✓ Connected: {len(ss)} schemas, {n} tables</span>')


@router.post("/ui/connections/save", response_class=HTMLResponse)
def ui_save(request: Request, conn_id: str = Form(""), name: str = Form(""), kind: str = Form("postgresql"), host: str = Form(""), port: str = Form("5432"),
            dbname: str = Form(""), user: str = Form(""), password_env: str = Form("")):
    c = _form_conn(conn_id, name, kind, host, port, dbname, user, password_env)
    with db() as d:
        if conn_id:
            d.execute("UPDATE connections SET name=?, kind=?, host=?, port=?, dbname=?, user=?, password_env=? WHERE id=?",
                      (c["name"], c["kind"], c["host"], c["port"], c["dbname"], c["user"], c["password_env"], conn_id))
        else:
            conn_id = str(uuid.uuid4())
            d.execute("INSERT INTO connections (id,name,kind,host,port,dbname,user,password_env,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (conn_id, c["name"], c["kind"], c["host"], c["port"], c["dbname"], c["user"], c["password_env"], now()))
    return ui_detail(request, conn_id)


@router.post("/ui/connections/{conn_id}/delete", response_class=HTMLResponse)
def ui_delete(request: Request, conn_id: str):
    with db() as d:
        d.execute("DELETE FROM connections WHERE id=?", (conn_id,))
    ctx = home_ctx()
    return templates.TemplateResponse(request, "conn_detail.html", {**ctx, "oob": True})


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
