"""coa-open minimal server: submit a linking job -> list mappings -> approve / reject -> export.

Run:  docker compose up --build        (everything: Postgres demo database + this app)
      or locally: pip install -r requirements.txt; ONTOP_HOME=/path/to/ontop-cli uvicorn server.app:app --port 8000
UI:   http://localhost:8000/
State lives in SQLite under COA_DATA_DIR (default ./data). Jobs run serially in a background thread.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "el_proto"))
import el_proto as el  # noqa: E402

DB_PATH = Path(os.environ.get("COA_DATA_DIR", str(ROOT / "data"))) / "coa.db"
DB_PATH.parent.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(ROOT / "server" / "templates"))
app = FastAPI(title="coa-open")
app.mount("/static", StaticFiles(directory=str(ROOT / "server" / "static")), name="static")  # vendored CSS/JS, no CDN
from server import drafts as _drafts  # noqa: E402  (ontology drafts: database -> LLM proposals -> review -> v1)
app.include_router(_drafts.router)
from server import connections as _conns  # noqa: E402  (saved database connections and schema browser)
app.include_router(_conns.router)
from server import requests as _reqs  # noqa: E402  (ask the LLM to edit a draft)
app.include_router(_reqs.router)

AUTO_ACCEPT = 0.9  # at or above: accepted automatically
REVIEW_MIN = 0.5  # from here up to AUTO_ACCEPT: human review. Below: novel (treated as no mapping)


# ──────────────────────────────── DB ────────────────────────────────


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, status TEXT, provider TEXT, retriever TEXT, k INTEGER,
              created_at TEXT, finished_at TEXT, error TEXT, summary TEXT);
            CREATE TABLE IF NOT EXISTS mappings (
              id TEXT PRIMARY KEY, job_id TEXT, kind TEXT, source TEXT, table_name TEXT,
              pred TEXT, confidence REAL, reason TEXT, inverse INTEGER,
              candidates TEXT, status TEXT, decided_uri TEXT, decided_at TEXT);
            """
        )


init_db()


# ──────────────────────────────── Linking ────────────────────────────────


def status_for(conf: float, pred: list[str]) -> str:
    if not pred:
        return "none"
    if conf >= AUTO_ACCEPT:
        return "auto"
    if conf >= REVIEW_MIN:
        return "review"
    return "novel"


def _uris(v) -> list[str]:
    if not v or v == "none":
        return []
    return [str(u) for u in (v if isinstance(v, list) else [v]) if u and u != "none"]


def run_job(job_id: str, provider: str, retriever: str, k: int) -> None:
    try:
        el.set_provider(provider)
        if not el.TARGETS:
            el.TARGETS = el.load_targets()
        tables = el.load_schema()
        cands = el.build_candidates(tables, k, retriever)
        rows = []
        for t in tables:
            r = el.judge(t, cands[t["name"]])
            tb = r.get("table", {})
            pred = _uris(tb.get("uri"))
            rows.append(("table", t["name"], t["name"], pred, float(tb.get("confidence") or 0), tb.get("reason", ""), 0, cands[t["name"]]["table"]))
            for c in r.get("columns", []):
                pred = _uris(c.get("uri"))
                rows.append(("column", f"{t['name']}.{c.get('name')}", t["name"], pred, float(c.get("confidence") or 0), c.get("reason", ""), 0, cands[t["name"]]["columns"].get(c.get("name"), [])))
            for fk in r.get("foreign_keys", []):
                cols = ",".join(fk.get("columns", []))
                ref = next((x["ref_table"] for x in t["fks"] if ",".join(x["columns"]) == cols), "?")
                pred = _uris(fk.get("uri"))
                rows.append(("fk", f"{t['name']}.{cols} -> {ref}", t["name"], pred, float(fk.get("confidence") or 0), fk.get("reason", ""), int(bool(fk.get("inverse"))), cands[t["name"]]["fks"].get(cols, [])))
        with db() as c:
            counts: dict[str, int] = {}
            for kind, source, tn, pred, conf, reason, inverse, cand_idx in rows:
                st = status_for(conf, pred)
                counts[st] = counts.get(st, 0) + 1
                cand = [{"uri": el.TARGETS[i]["uri"], "label": el.TARGETS[i]["label"], "src": el.TARGETS[i]["src"]} for i in cand_idx[:10]]
                c.execute(
                    "INSERT INTO mappings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), job_id, kind, source, tn, json.dumps(pred), conf, reason, inverse, json.dumps(cand), st,
                     json.dumps(pred) if st == "auto" else None, None),
                )
            c.execute("UPDATE jobs SET status='done', finished_at=?, summary=? WHERE id=?", (now(), json.dumps(counts), job_id))
    except Exception:  # noqa: BLE001
        with db() as c:
            c.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id=?", (now(), traceback.format_exc(), job_id))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ──────────────────────────────── API ────────────────────────────────


class JobIn(BaseModel):
    provider: str = "deepseek"
    retriever: str = "bm25"
    k: int = 20


@app.post("/api/jobs")
def create_job(body: JobIn) -> dict:
    job_id = str(uuid.uuid4())
    with db() as c:
        c.execute("INSERT INTO jobs (id,status,provider,retriever,k,created_at) VALUES (?,?,?,?,?,?)",
                  (job_id, "running", body.provider, body.retriever, body.k, now()))
    threading.Thread(target=run_job, args=(job_id, body.provider, body.retriever, body.k), daemon=True).start()
    return {"id": job_id, "status": "running"}


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY created_at DESC")]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    return dict(r)


@app.get("/api/jobs/{job_id}/mappings")
def list_mappings(job_id: str, status: str | None = None) -> list[dict]:
    q = "SELECT * FROM mappings WHERE job_id=?" + (" AND status=?" if status else "") + " ORDER BY table_name, kind, source"
    with db() as c:
        rows = c.execute(q, (job_id, status) if status else (job_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pred"] = json.loads(d["pred"])
        d["candidates"] = json.loads(d["candidates"])
        d["decided_uri"] = json.loads(d["decided_uri"]) if d["decided_uri"] else None
        out.append(d)
    return out


class Decision(BaseModel):
    decision: str  # approve | reject
    uri: list[str] | None = None  # to replace the mapping target on approve


@app.post("/api/mappings/{mapping_id}/decision")
def decide(mapping_id: str, body: Decision) -> dict:
    return _decide(mapping_id, body.decision, body.uri)


def _decide(mapping_id: str, decision: str, uri: list[str] | None) -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM mappings WHERE id=?", (mapping_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        if decision == "approve":
            final = uri if uri is not None else json.loads(r["pred"])
            c.execute("UPDATE mappings SET status='approved', decided_uri=?, decided_at=? WHERE id=?", (json.dumps(final), now(), mapping_id))
        elif decision == "reject":
            c.execute("UPDATE mappings SET status='rejected', decided_uri=NULL, decided_at=? WHERE id=?", (now(), mapping_id))
        else:
            raise HTTPException(400, "decision must be approve or reject")
        return dict(c.execute("SELECT id,status,decided_uri FROM mappings WHERE id=?", (mapping_id,)).fetchone())


@app.get("/api/jobs/{job_id}/export.ttl", response_class=PlainTextResponse)
def export_ttl(job_id: str) -> str:
    """Export accepted (auto + approved) mappings as Turtle. table -> class, column -> datatype property, FK -> object property."""
    with db() as c:
        rows = c.execute("SELECT * FROM mappings WHERE job_id=? AND status IN ('auto','approved') ORDER BY table_name, kind", (job_id,)).fetchall()
    base = "urn:coa-open:schema:"
    lines = [
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix coa: <urn:coa-open:vocab:> .",
        "",
    ]
    for r in rows:
        uris = json.loads(r["decided_uri"] or r["pred"])
        if not uris:
            continue
        subj = f"<{base}{r['source'].replace(' ', '')}>"
        pred = {"table": "coa:mapsToClass", "column": "coa:mapsToProperty", "fk": "coa:mapsToObjectProperty"}[r["kind"]]
        for u in uris:
            lines.append(f"{subj} {pred} <{u}> ;")
            lines.append(f"    coa:confidence {r['confidence']:.2f} ; coa:status \"{r['status']}\"" + (" ; coa:inverse true" if r["inverse"] else "") + " .")
    return "\n".join(lines) + "\n"


XSD = {"int": "xsd:integer", "bigint": "xsd:integer", "decimal": "xsd:decimal", "datetime": "xsd:dateTime", "date": "xsd:date"}


def _xsd(coltype: str) -> str:
    return XSD.get(coltype.split("(")[0], "xsd:string")


@app.get("/api/jobs/{job_id}/export.obda", response_class=PlainTextResponse)
def export_obda(job_id: str) -> str:
    """Generate Ontop OBDA mappings from the accepted mappings.

    table -> class:                :{T}/{pk} a <Class>             <- SELECT pk FROM T
    column -> datatype property:   :{T}/{pk} <prop> {col}^^xsd     <- SELECT pk, col FROM T
    FK -> object property:         :{T}/{pk} <prop> :{R}/{refcol}  <- SELECT pk, fkcol FROM T  (inverse swaps subject and object)
    Role-dependent tables (one table -> several classes) are emitted several times with a TODO, because the WHERE condition value is unknown.
    """
    tables = {t["name"]: t for t in el.load_schema()}
    with db() as c:
        rows = c.execute("SELECT * FROM mappings WHERE job_id=? AND status IN ('auto','approved') ORDER BY table_name, kind", (job_id,)).fetchall()

    def iri(tname: str, cols: list[str]) -> str:
        return f":{tname}/" + "/".join("{" + col + "}" for col in cols)

    def pk(tname: str) -> list[str]:
        t = tables.get(tname)
        return t["pk"] if t and t["pk"] else [tables[tname]["columns"][0]["name"]]

    out = [
        "[PrefixDeclaration]",
        ":\t\turn:coa-open:data:",
        "in:\t\thttp://data.world/schema/insurance/",
        "schema:\thttps://schema.org/",
        "xsd:\t\thttp://www.w3.org/2001/XMLSchema#",
        "",
        "[MappingDeclaration] @collection [[",
    ]
    n = 0
    for r in rows:
        uris = json.loads(r["decided_uri"] or r["pred"])
        if not uris:
            continue
        tn = r["table_name"]
        if tn not in tables:
            continue
        subj = iri(tn, pk(tn))
        pkcols = ", ".join(f'"{col}"' for col in pk(tn))
        for u in uris:
            n += 1
            mid = f"m{n}_{r['kind']}_{r['source'].replace('.', '_').replace(',', '_').replace(' -> ', '__')}"
            note = ""
            if r["kind"] == "table":
                if len(uris) > 1:
                    note = f"  -- TODO: {tn} splits into {len(uris)} classes. Decide the WHERE condition that routes rows"
                target = f"{subj} a <{u}> ."
                source = f'SELECT {pkcols} FROM "{tn}"{note}'
            elif r["kind"] == "column":
                col = r["source"].split(".", 1)[1]
                ctype = next((c["type"] for c in tables[tn]["columns"] if c["name"] == col), "")
                target = f"{subj} <{u}> {{{col}}}^^{_xsd(ctype)} ."
                source = f'SELECT {pkcols}, "{col}" FROM "{tn}"'
            else:
                cols_part, ref = r["source"].split(".", 1)[1].split(" -> ")
                fkcols = cols_part.split(",")
                fk = next((f for f in tables[tn]["fks"] if f["columns"] == fkcols), None)
                if not fk:
                    continue
                obj = iri(fk["ref_table"], fk["ref_columns"])
                # Assumes the referenced IRI is built from the referenced table's PK. Check when the FK does not target that PK
                if fk["ref_table"] in tables and sorted(fk["ref_columns"]) != tables[fk["ref_table"]]["pk"]:
                    note = f"  -- TODO: referenced columns do not match the PK of {fk['ref_table']}"
                # The object IRI template uses the FK column values
                obj = f":{fk['ref_table']}/" + "/".join("{" + col + "}" for col in fkcols)
                target = f"{obj} <{u}> {subj} ." if r["inverse"] else f"{subj} <{u}> {obj} ."
                source = f'SELECT {pkcols}, ' + ", ".join(f'"{col}"' for col in fkcols) + f' FROM "{tn}"{note}'
            out += [f"mappingId\t{mid}", f"target\t\t{target}", f"source\t\t{source}", ""]
    out.append("]]")
    return "\n".join(out) + "\n"


# ──────────────────────────────── UI (HTMX) ────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def ui_index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"jobs": list_jobs(), "drafts": _drafts.list_drafts(), **_conns.home_ctx()})


@app.post("/ui/jobs")
def ui_create_job(provider: str = Form("deepseek"), retriever: str = Form("bm25"), k: int = Form(20)):
    job = create_job(JobIn(provider=provider, retriever=retriever, k=k))
    return RedirectResponse(f"/ui/jobs/{job['id']}", status_code=303)


@app.get("/ui/jobs/{job_id}", response_class=HTMLResponse)
def ui_job(request: Request, job_id: str, status: str = "review"):
    job = get_job(job_id)
    rows = list_mappings(job_id, None if status == "all" else status)
    with db() as c:
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM mappings WHERE job_id=? GROUP BY status", (job_id,))}
    return templates.TemplateResponse(request, "job.html", {"job": job, "rows": rows, "status": status, "counts": counts})


@app.post("/ui/mappings/{mapping_id}/decision", response_class=HTMLResponse)
def ui_decide(request: Request, mapping_id: str, decision: str = Form(...), uri: str = Form("")):
    uris = [u.strip() for u in uri.split(",") if u.strip()] or None
    _decide(mapping_id, decision, uris)
    row = next(r for r in list_mappings(_job_of(mapping_id)) if r["id"] == mapping_id)
    return templates.TemplateResponse(request, "row.html", {"r": row})


def _job_of(mapping_id: str) -> str:
    with db() as c:
        return c.execute("SELECT job_id FROM mappings WHERE id=?", (mapping_id,)).fetchone()[0]
