"""Ontology draft: customer database -> catalog extraction -> Ontop bootstrap -> LLM pruning and naming -> human review -> v1.ttl / v1.obda.

Flow (serial, in a background thread):
  1. scan/extract_pg.py turns information_schema and column profiles into a catalog JSON
  2. ontop bootstrap builds the Direct Mapping (boot.obda)
  3. dm_proto.run_pipeline() builds the proposed model
  4. classes / value-dependent subclasses / datatype properties / object properties are expanded into draft_items (confidence >= 0.9 is auto, the rest review; columns the LLM dropped become pruned and can be restored)
  5. a person edits names, labels and descriptions and approves or rejects -> only approved items go into v1.ttl / v1.obda
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dm_proto"))
import dm_proto as dmp  # noqa: E402
import el_proto as el  # noqa: E402  (dm_proto adds el_proto to sys.path)
from server import env as E  # noqa: E402  (.env loading, PGPASSWORD handling)

DATA_DIR = Path(os.environ.get("TABULARY_DATA_DIR", str(ROOT / "data")))
DB_PATH = DATA_DIR / "tabulary.db"
ONTOP_HOME = Path(os.environ.get("ONTOP_HOME", "/opt/ontop"))  # directory that contains the `ontop` launcher
WORK = DATA_DIR / "drafts"
WORK.mkdir(parents=True, exist_ok=True)
if not (ONTOP_HOME / "ontop").exists():
    print(f"warning: Ontop CLI not found in {ONTOP_HOME}; set ONTOP_HOME", file=sys.stderr)
templates = Jinja2Templates(directory=str(ROOT / "server" / "templates"))
router = APIRouter()
AUTO_ACCEPT = 0.9
DEFAULT_CONNINFO = f"host={os.environ.get('DEMO_DB_HOST', 'localhost')} port={os.environ.get('DEMO_DB_PORT', '5433')} dbname=tabulary user=tabulary"  # password: PGPASSWORD / DEMO_DB_PASSWORD


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


with db() as _c:
    _c.executescript(
        """
        CREATE TABLE IF NOT EXISTS drafts (
          id TEXT PRIMARY KEY, status TEXT, created_at TEXT, finished_at TEXT, error TEXT, step TEXT,
          conninfo TEXT, schema_name TEXT, provider TEXT, stats_level TEXT, domain TEXT, ns TEXT,
          boot_obda TEXT, model TEXT, summary TEXT);
        CREATE TABLE IF NOT EXISTS draft_items (
          id TEXT PRIMARY KEY, draft_id TEXT, kind TEXT, table_name TEXT, key TEXT, ref TEXT,
          name TEXT, label TEXT, comment TEXT, confidence REAL, detail TEXT,
          status TEXT, decided_name TEXT, decided_label TEXT, decided_comment TEXT, decided_at TEXT);
        """
    )
    for _col in ("progress_done INTEGER", "progress_total INTEGER", "connection_id TEXT"):
        try:
            _c.execute(f"ALTER TABLE drafts ADD COLUMN {_col}")
        except sqlite3.OperationalError:
            pass  # column already exists
    # drafts made before connection_id existed show "Name (user@host:port/db)"; recover the saved connection from that
    try:
        _c.execute("""UPDATE drafts SET connection_id = (SELECT id FROM connections WHERE drafts.conninfo LIKE connections.name || ' (%')
                      WHERE connection_id IS NULL AND EXISTS (SELECT 1 FROM connections WHERE drafts.conninfo LIKE connections.name || ' (%')""")
    except sqlite3.OperationalError:
        pass  # fresh database: no connections table yet, nothing to recover


# ──────────────────────────────── Run ────────────────────────────────


def _mask(conninfo: str) -> str:
    return re.sub(r"(password=)\S+", r"\1***", conninfo)


def _jdbc_props(conninfo: str, password: str | None) -> str:
    kv = dict(p.split("=", 1) for p in shlex.split(conninfo) if "=" in p)
    url = f"jdbc:postgresql://{kv.get('host', 'localhost')}:{kv.get('port', '5432')}/{kv.get('dbname', kv.get('user', ''))}"
    return f"jdbc.url={url}\njdbc.user={kv.get('user', '')}\njdbc.password={password or ''}\njdbc.driver=org.postgresql.Driver\n"


def _set_step(draft_id: str, step: str, done: int | None = None, total: int | None = None) -> None:
    with db() as c:
        c.execute("UPDATE drafts SET step=?, progress_done=?, progress_total=? WHERE id=?", (step, done, total, draft_id))


def run_draft(draft_id: str, conninfo: str, password: str | None, schema: str, provider: str, stats_level: str, domain: str | None, ns: str) -> None:
    """conninfo carries no password; it goes to psql as PGPASSWORD and to Ontop in a 0600 properties file that is deleted right after bootstrap."""
    wd = WORK / draft_id
    wd.mkdir(exist_ok=True)
    jdbc = wd / "jdbc.properties"
    try:
        _set_step(draft_id, "1/3 catalog extraction (information_schema + profile)")
        cmd = [sys.executable, str(ROOT / "scan" / "extract_pg.py"), conninfo, "--schema", schema, "-o", str(wd / "catalog.json")]
        if stats_level != "values":
            cmd.append("--no-values")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, env=E.pg_env(password))
        if r.returncode != 0:
            raise RuntimeError("extract_pg failed: " + r.stderr[-2000:])

        _set_step(draft_id, "2/3 Ontop bootstrap (Direct Mapping)")
        jdbc.touch(mode=0o600)
        jdbc.write_text(_jdbc_props(conninfo, password))
        try:
            r = subprocess.run(["bash", "./ontop", "bootstrap", "-b", "urn:tabulary:boot:/", "-p", str(jdbc), "-m", str(wd / "boot.obda"), "-t", str(wd / "boot.owl")],
                               cwd=ONTOP_HOME, capture_output=True, text=True, timeout=1800)
        finally:
            jdbc.unlink(missing_ok=True)
        if r.returncode != 0 or not (wd / "boot.obda").exists():
            raise RuntimeError("ontop bootstrap failed: " + (r.stderr or r.stdout)[-2000:])

        _set_step(draft_id, "3/3 LLM proposals (one table at a time)")
        el.set_provider(provider)
        tables, stats = dmp.load_catalog(wd / "catalog.json")
        n_tables, seen = len(tables), [0]
        _set_step(draft_id, f"3/3 LLM proposals: table 0 of {n_tables}", 0, n_tables)

        def log(m: str) -> None:
            if m.startswith("  ") and ":" in m:  # one line per finished table
                seen[0] += 1
                _set_step(draft_id, f"3/3 LLM proposals: table {seen[0]} of {n_tables} ({m.strip().split(':', 1)[0]})", seen[0], n_tables)
            elif m.startswith("harmonize"):
                _set_step(draft_id, "3/3 harmonizing names across the ontology", n_tables, n_tables)
        res = dmp.run_pipeline(tables, stats, wd / "boot.obda", level=stats_level, domain=domain or None, schema=schema, ns=ns, do_harmonize=True, log=log)
        model, props = res["model"], res["props"]
        items = _items_from_model(draft_id, model, props, res["dm"])
        counts: dict[str, int] = {}
        with db() as c:
            for it in items:
                counts[it[11]] = counts.get(it[11], 0) + 1
                c.execute("INSERT INTO draft_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", it)
            c.execute("UPDATE drafts SET status='done', finished_at=?, step=NULL, boot_obda=?, model=?, summary=? WHERE id=?",
                      (now(), (wd / "boot.obda").read_text(), json.dumps(model, ensure_ascii=False), json.dumps(counts), draft_id))
    except Exception:  # noqa: BLE001
        with db() as c:
            c.execute("UPDATE drafts SET status='failed', finished_at=?, error=? WHERE id=?", (now(), traceback.format_exc()[-4000:], draft_id))


def _status(conf) -> str:
    try:
        return "auto" if float(conf) >= AUTO_ACCEPT else "review"
    except (TypeError, ValueError):
        return "review"


SHARED = "~SHARED"  # pseudo table name grouping proposals that span several tables


def _items_from_model(draft_id: str, model: dict, props: dict, dm: dict) -> list[tuple]:
    """Model -> draft_items rows. ref points back into the model so export can rebuild it."""
    rows = []
    cls = model["classes"]
    props_by_tu = {k.upper(): v for k, v in props.items()}

    def add(kind, table, key, ref, name, label, comment, conf, detail, status):
        rows.append((str(uuid.uuid4()), draft_id, kind, table, key, json.dumps(ref), name, label or "", comment or "", conf, json.dumps(detail, ensure_ascii=False), status, None, None, None, None))

    for i, sc in enumerate(model.get("superclasses") or []):
        add("superclass", SHARED, f"superclass {sc['name']}", {"superclass": i}, sc["name"], sc["label"], sc["comment"], sc.get("confidence"),
            {"kind": "proposed superclass", "members": [cls[m]["name"] for m in sc["members"]], "properties": sc["properties"]}, "review")
    for ck, c in cls.items():
        if c.get("stub"):
            add("class", ck, ck, {"class": ck}, c["name"], c["label"], c["comment"], 1.0, {"kind": "referenced (out of scope)"}, "auto")
        elif c.get("filter"):
            add("subclass", c["parent"], ck, {"class": ck}, c["name"], c["label"], c["comment"], c.get("confidence"), {"kind": "value subclass", "where": f"{c['filter'][0]} = '{c['filter'][1]}'", "parent": cls[c["parent"]]["name"]}, "review")
        else:
            add("class", ck, ck, {"class": ck}, c["name"], c["label"], c["comment"], c.get("confidence"), {"kind": c.get("kind"), "parent": cls[c["parent"]]["name"] if c.get("parent") in cls else None}, _status(c.get("confidence")))
    for i, p in enumerate(model["dtprops"]):
        add("dtprop", p["table"], f"{p['table']}.{p['col']}", {"dtprop": i}, p["name"], p["label"], p["comment"], p.get("confidence"), {"column": p["col_sql"], "xsd": p["xsd"]}, _status(p.get("confidence")))
    kept_cols = {(p["table"], p["col"]) for p in model["dtprops"]}
    for tu, d in dm.items():
        if tu not in cls or cls[tu].get("stub"):
            continue
        llm_cols = {c["name"].upper(): c for c in (props_by_tu.get(tu, {}).get("columns") or [])}
        for cu, col in d["cols"].items():
            if (tu, cu) in kept_cols:
                continue
            lc = llm_cols.get(cu, {})
            default = re.sub(r"_(\w)", lambda m: m.group(1).upper(), col["name"].lower())
            add("dtprop", tu, f"{tu}.{cu}", {"new_dtprop": {"table": tu, "col": cu, "col_sql": col["name"], "xsd": col["xsd"]}}, lc.get("property_name") or default, lc.get("label") or "", lc.get("description") or "",
                lc.get("confidence"), {"column": col["name"], "xsd": col["xsd"], "technical": "LLM: identifier / key column" if not lc or not lc.get("keep") else "join table column"}, "review")
    for i, p in enumerate(model["objprops"]):
        key = f"{p['table']}.{','.join(p['cols'])} -> {p['range_t']}" if not p["join"] else f"{p['table']} (join) {p['domain_t']} -> {p['range_t']}"
        add("objprop", p["table"], key, {"objprop": i}, p["name"], p["label"], p["comment"], p.get("confidence"), {"domain": cls[p["domain_t"]]["name"], "range": cls[p["range_t"]]["name"], "join": bool(p["join"])}, _status(p.get("confidence")))
    return rows


# ──────────────────────────────── Rebuild the model from review decisions and export ────────────────────────────────


def _final_model(draft_id: str) -> tuple[dict, dict]:
    with db() as c:
        d = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not d or d["status"] != "done":
            raise HTTPException(409, "draft is not done")
        items = [dict(r) for r in c.execute("SELECT * FROM draft_items WHERE draft_id=?", (draft_id,))]
    model = json.loads(d["model"])
    with tempfile.NamedTemporaryFile("w", suffix=".obda", delete=False) as f:
        f.write(d["boot_obda"])
    dm = dmp.direct_mapping(dmp.parse_obda(Path(f.name), d["schema_name"]))
    Path(f.name).unlink()

    def apply(entry: dict, it: dict) -> dict:
        return {**entry, "name": it["decided_name"] or it["name"], "label": it["decided_label"] if it["decided_label"] is not None else it["label"],
                "comment": it["decided_comment"] if it["decided_comment"] is not None else it["comment"]}

    # Export rule: structure taken from the database (tables, columns, foreign-key relations) is exported unless rejected;
    # unreviewed items carry a review-status mark. Structure the LLM added (value subclasses, superclasses) needs approval.
    # "pruned" only exists in drafts created before columns stopped being dropped; those stay out.
    def included(it: dict) -> bool:
        if it["kind"] in ("subclass", "superclass"):
            return it["status"] == "approved"
        return it["status"] in ("auto", "approved", "review")

    def mark(entry: dict, it: dict) -> dict:
        detail = json.loads(it["detail"]) if it.get("detail") else {}
        return {**entry, "unreviewed": it["status"] == "review", "technical": detail.get("technical")}

    classes = {}
    for it in items:
        if it["kind"] in ("class", "subclass") and included(it):
            ck = json.loads(it["ref"])["class"]
            classes[ck] = mark(apply(model["classes"][ck], it), it)
    # Subclasses / subtypes whose parent was dropped lose their parent link
    for ck, c in classes.items():
        if c.get("filter") and c["parent"] not in classes:
            c["drop"] = True
        elif c.get("parent") and c["parent"] not in classes:
            c["parent"] = None
    classes = {k: v for k, v in classes.items() if not v.get("drop")}
    # approved superclasses: a class with no table of its own; members get it as an extra parent and shared properties get it as domain
    super_props: dict[str, str] = {}
    for it in items:
        if it["kind"] == "superclass" and included(it):
            sc = model["superclasses"][json.loads(it["ref"])["superclass"]]
            key = f"{SHARED}:{sc['name']}"
            classes[key] = apply({"name": sc["name"], "label": sc["label"], "comment": sc["comment"], "parent": None}, it)
            for m in sc["members"]:
                if m in classes:
                    classes[m].setdefault("extra_parents", []).append(key)
            for pn in sc["properties"]:
                super_props[pn] = key
    dtprops, objprops = [], []
    for it in items:
        if not included(it) or it["kind"] in ("class", "subclass", "superclass"):
            continue
        ref = json.loads(it["ref"])
        if "dtprop" in ref:
            p = mark(apply(model["dtprops"][ref["dtprop"]], it), it)
            if p["table"] in classes:
                dtprops.append(p)
        elif "new_dtprop" in ref:
            p = mark(apply({**ref["new_dtprop"], "label": "", "comment": "", "confidence": None}, it), it)
            if p["table"] in classes:
                dtprops.append(p)
        elif "objprop" in ref:
            p = mark(apply(model["objprops"][ref["objprop"]], it), it)
            if p["domain_t"] in classes and p["range_t"] in classes and (p["join"] or p["table"] in classes):
                objprops.append(p)
    for p in dtprops:
        if p["name"] in super_props:
            p["domain_class"] = super_props[p["name"]]
    final = {"classes": classes, "dtprops": dtprops, "objprops": objprops, "pruned": [], "ns": model["ns"], "subtype_of": model.get("subtype_of", {})}
    return final, dm


def export(draft_id: str, what: str) -> str:
    final, dm = _final_model(draft_id)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / what
        if what == "v1.ttl":
            dmp.write_ttl(final, p)
        else:
            with db() as c:
                ns = c.execute("SELECT ns FROM drafts WHERE id=?", (draft_id,)).fetchone()["ns"]
            dmp.write_obda(final, dm, p, ns.rsplit("/", 2)[0] + "/data/")
        return p.read_text()


# ──────────────────────────────── API ────────────────────────────────


class DraftIn(BaseModel):
    conninfo: str = DEFAULT_CONNINFO
    connection_id: str | None = None  # a saved connection; overrides conninfo
    schema_name: str = "public"
    provider: str = "deepseek"
    stats_level: str = "counts"  # none | counts (external LLM) | values (in-house LLM)
    domain: str | None = None
    ns: str = "http://example.org/ontology/v1/"


@router.post("/api/drafts")
def create_draft(body: DraftIn) -> dict:
    did = str(uuid.uuid4())
    ci, pw = E.split_password(body.conninfo)  # a password given inline is moved to the environment, never shown or stored
    if pw is None:
        pw = os.environ.get("PGPASSWORD") or os.environ.get("DEMO_DB_PASSWORD")
    shown = ci
    if body.connection_id:
        from server import connections as _conns
        saved = _conns.get(body.connection_id)
        ci, shown = _conns.conninfo(saved), f"{saved['name']} ({saved['user']}@{saved['host']}:{saved['port']}/{saved['dbname']})"
        try:
            pw = E.password_from_env(saved.get("password_env"))
        except RuntimeError as e:
            raise HTTPException(400, str(e))
    with db() as c:
        c.execute("INSERT INTO drafts (id,status,created_at,step,conninfo,connection_id,schema_name,provider,stats_level,domain,ns) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (did, "running", now(), "queued", shown, body.connection_id, body.schema_name, body.provider, body.stats_level, body.domain, body.ns))
    threading.Thread(target=run_draft, args=(did, ci, pw, body.schema_name, body.provider, body.stats_level, body.domain, body.ns), daemon=True).start()
    return {"id": did, "status": "running"}


@router.get("/api/drafts")
def list_drafts() -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,status,created_at,finished_at,step,conninfo,schema_name,provider,stats_level,domain,summary,error FROM drafts ORDER BY created_at DESC")]


@router.get("/api/drafts/{draft_id}")
def get_draft(draft_id: str) -> dict:
    with db() as c:
        r = c.execute("SELECT id,status,created_at,finished_at,step,progress_done,progress_total,conninfo,schema_name,provider,stats_level,domain,ns,summary,error FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    return dict(r)


@router.get("/api/drafts/{draft_id}/items")
def list_items(draft_id: str, status: str | None = None, table: str | None = None) -> list[dict]:
    q = "SELECT * FROM draft_items WHERE draft_id=?"
    args: list = [draft_id]
    if status:
        q += " AND status=?"
        args.append(status)
    if table:
        q += " AND table_name=?"
        args.append(table)
    q += " ORDER BY table_name, CASE kind WHEN 'class' THEN 0 WHEN 'subclass' THEN 1 WHEN 'dtprop' THEN 2 ELSE 3 END, key"
    with db() as c:
        out = []
        for r in c.execute(q, args):
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
            out.append(d)
        return out


class ItemDecision(BaseModel):
    decision: str  # approve | reject | reset
    name: str | None = None
    label: str | None = None
    comment: str | None = None


@router.post("/api/draft-items/{item_id}/decision")
def decide_item(item_id: str, body: ItemDecision) -> dict:
    return _decide(item_id, body.decision, body.name, body.label, body.comment)


def _decide(item_id: str, decision: str, name, label, comment) -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM draft_items WHERE id=?", (item_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        if decision == "approve":
            if name is not None and not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", name):
                raise HTTPException(400, "name must be an identifier (letters, digits, underscore)")
            c.execute("UPDATE draft_items SET status='approved', decided_name=?, decided_label=?, decided_comment=?, decided_at=? WHERE id=?",
                      (name or None, label, comment, now(), item_id))
        elif decision == "reject":
            c.execute("UPDATE draft_items SET status='rejected', decided_at=? WHERE id=?", (now(), item_id))
        elif decision == "reset":
            ref = json.loads(r["ref"])
            back = "pruned" if ref.get("new_dtprop") else ("review" if "superclass" in ref else _status(r["confidence"]))  # proposals always need a human
            c.execute("UPDATE draft_items SET status=?, decided_name=NULL, decided_label=NULL, decided_comment=NULL, decided_at=NULL WHERE id=?",
                      (back, item_id))
        else:
            raise HTTPException(400, "decision must be approve | reject | reset")
        d = dict(c.execute("SELECT * FROM draft_items WHERE id=?", (item_id,)).fetchone())
        d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
        return d


@router.get("/api/drafts/{draft_id}/v1.ttl", response_class=PlainTextResponse)
def export_ttl(draft_id: str) -> str:
    return export(draft_id, "v1.ttl")


@router.get("/api/drafts/{draft_id}/v1.obda", response_class=PlainTextResponse)
def export_obda(draft_id: str) -> str:
    return export(draft_id, "v1.obda")


# ──────────────────────────────── UI ────────────────────────────────


def _class_list(draft_id: str) -> list[dict]:
    """One entry per table: the class name the reviewer sees, how many items still need review, and the total."""
    with db() as c:
        rows = c.execute(
            """SELECT table_name,
                      SUM(status = 'review') AS review,
                      COUNT(*) AS total,
                      MAX(CASE WHEN kind = 'class' AND key = table_name THEN COALESCE(decided_name, name) END) AS class_name,
                      MAX(CASE WHEN kind = 'class' AND key = table_name THEN detail END) AS class_detail
               FROM draft_items WHERE draft_id = ? GROUP BY table_name""", (draft_id,)).fetchall()
    out = []
    for r in rows:
        detail = json.loads(r["class_detail"]) if r["class_detail"] else {}
        shared = r["table_name"] == SHARED
        out.append({"table": r["table_name"], "class_name": "Shared concepts" if shared else (r["class_name"] or r["table_name"].lower()),
                    "review": r["review"] or 0, "total": r["total"], "referenced": detail.get("kind") == "referenced (out of scope)", "shared": shared})
    # shared proposals first, then in-scope classes, then referenced stubs; alphabetical inside each group
    return sorted(out, key=lambda x: (not x["shared"], x["referenced"], x["class_name"].lower()))


def _panel_ctx(draft_id: str, status: str, table: str | None) -> dict:
    d = get_draft(draft_id)
    ctx = {"d": d, "status": status, "table": table, "items": [], "counts": {}, "classes": [], "current": None, "ask_history": []}
    if d["status"] == "done":
        classes = _class_list(draft_id)
        if not table or table not in {x["table"] for x in classes}:
            table = next((x["table"] for x in classes if x["review"]), classes[0]["table"] if classes else None)
        ctx["classes"], ctx["table"] = classes, table
        ctx["current"] = next((x for x in classes if x["table"] == table), None)
        with db() as c:
            try:
                ctx["ask_history"] = [dict(r, ops=json.loads(r["operations"] or "[]")) for r in c.execute(
                    "SELECT * FROM draft_requests WHERE draft_id=? AND table_name=? ORDER BY created_at DESC LIMIT 8", (draft_id, table))]
            except sqlite3.OperationalError:
                ctx["ask_history"] = []
        ctx["items"] = list_items(draft_id, None if status == "all" else status, table)
        with db() as c:
            ctx["counts"] = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) n FROM draft_items WHERE draft_id=? AND table_name=? GROUP BY status", (draft_id, table))}
    return ctx


@router.post("/ui/drafts", response_class=HTMLResponse)
def ui_create(request: Request, connection_id: str = Form(""), conninfo: str = Form(DEFAULT_CONNINFO), schema_name: str = Form("public"), provider: str = Form("deepseek"),
              stats_level: str = Form("counts"), domain: str = Form("")):
    d = create_draft(DraftIn(connection_id=connection_id or None, conninfo=conninfo, schema_name=schema_name, provider=provider, stats_level=stats_level, domain=domain or None))
    if request.headers.get("HX-Request"):
        resp = templates.TemplateResponse(request, "draft_panel.html", _panel_ctx(d["id"], "all", None))
        resp.headers["HX-Push-Url"] = f"/ui/drafts/{d['id']}"
        return resp
    return RedirectResponse(f"/ui/drafts/{d['id']}", status_code=303)


@router.get("/ui/drafts/{draft_id}/panel", response_class=HTMLResponse)
def ui_panel(request: Request, draft_id: str, status: str = "all", table: str | None = None):
    return templates.TemplateResponse(request, "draft_panel.html", _panel_ctx(draft_id, status, table))


@router.get("/ui/drafts/{draft_id}", response_class=HTMLResponse)
def ui_draft(request: Request, draft_id: str, status: str = "all", table: str | None = None):
    """Direct link: the home page with this draft loaded in the panel."""
    from server import connections as _conns
    return templates.TemplateResponse(request, "index.html", {"drafts": list_drafts(), **_conns.home_ctx(), **_panel_ctx(draft_id, status, table)})


@router.post("/ui/draft-items/{item_id}/decision", response_class=HTMLResponse)
def ui_decide(request: Request, item_id: str, decision: str = Form(...), name: str = Form(""), label: str = Form(""), comment: str = Form(""),
              view_status: str = Form("all"), view_table: str = Form("")):
    """Returns the updated row (or nothing if it no longer belongs to the current tab), plus the tab counts and the
    class list as out-of-band swaps, so the whole view updates without a reload."""
    it = _decide(item_id, decision, name.strip() or None, label.strip() or None, comment.strip() or None)
    ctx = _panel_ctx(it["draft_id"], view_status, view_table or it["table_name"])
    row = templates.get_template("draft_row.html").render(it=it) if view_status in ("all", it["status"]) else ""
    head = templates.get_template("detail_head.html").render(**ctx, oob=True)
    cls = templates.get_template("class_list.html").render(**ctx, oob=True)
    return HTMLResponse(row + head + cls)


@router.get("/ui/drafts/{draft_id}/classes", response_class=HTMLResponse)
def ui_classes(request: Request, draft_id: str, table: str | None = None, status: str = "all"):
    return templates.TemplateResponse(request, "class_list.html", {"d": get_draft(draft_id), "classes": _class_list(draft_id), "table": table, "status": status})


@router.post("/ui/drafts/{draft_id}/tables/{table}/approve-all", response_class=HTMLResponse)
def ui_approve_all(request: Request, draft_id: str, table: str, status: str = Form("all")):
    """Approve every item of one table that is still waiting for review (auto items stay auto)."""
    with db() as c:
        c.execute("UPDATE draft_items SET status='approved', decided_at=? WHERE draft_id=? AND table_name=? AND status='review'", (now(), draft_id, table))
    return templates.TemplateResponse(request, "draft_panel.html", _panel_ctx(draft_id, status, table))
