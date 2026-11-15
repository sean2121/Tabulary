"""Ask the LLM to edit a draft: a reviewer writes a request ("1=full professor, 2=associate ..."), the LLM proposes
operations on the draft items of one table, the reviewer sees before/after and applies or discards.

Operations the LLM may propose (nothing else is accepted):
  update               change name / label / comment of an existing item
  reject               take an existing item out of the ontology
  add_value_subclass   add a subclass of the table's class selected by a column value (WHERE column = value)
Applied changes count as approved. Every request and its outcome is kept as history.
"""
from __future__ import annotations

import json
import re
import threading
import traceback
import uuid

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from server import drafts as D

router = APIRouter()
templates = D.templates
dmp = D.dmp
el = D.el

with D.db() as _c:
    _c.execute("""CREATE TABLE IF NOT EXISTS draft_requests (
        id TEXT PRIMARY KEY, draft_id TEXT, table_name TEXT, item_id TEXT, comment TEXT, status TEXT,
        message TEXT, operations TEXT, error TEXT, created_at TEXT, decided_at TEXT)""")

SYSTEM = """You help a data steward edit a draft business ontology generated from a relational database.
You get the draft items of one table (its class, value subclasses, datatype properties, object properties), the table's
columns with types, and the steward's request. Propose the smallest set of edits that fulfils the request.
Allowed operations:
- update: change name / label / comment of an existing item (omit fields you do not change). Class names UpperCamelCase,
  property names lowerCamelCase. Descriptions are one plain business sentence; no implementation details such as IRIs.
- reject: take an existing item out of the ontology.
- add_value_subclass: add a subclass of this table's class, selected by the value of one of the table's columns
  (value exactly as stored in the database). Use this when the steward explains what code values mean.
- add_relation: add an object property from this table's class to another table's class when the database has no
  foreign key for it (an implicit join). Give this table's columns and the matching primary-key columns of the other
  table, in the same order; the other tables and their primary keys are listed. Property name is a lowerCamelCase
  verb phrase (isPartOfPolicy, belongsToCustomer).
Refer to existing items only by the ids given (i1, i2, ...). Never invent columns or items. If the request cannot be done
with these operations, return no operations and say why in "message".
Output raw JSON only:
{"message": "<one or two sentences for the steward>",
 "operations": [{"op": "update", "item": "i3", "name": "...", "label": "...", "comment": "..."},
                {"op": "reject", "item": "i4", "reason": "..."},
                {"op": "add_value_subclass", "column": "...", "value": "...", "class_name": "...", "label": "...", "comment": "..."},
                {"op": "add_relation", "columns": ["..."], "ref_table": "...", "ref_columns": ["..."], "property_name": "...", "label": "...", "comment": "..."}]}"""

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _catalog(draft_id: str) -> list[dict]:
    try:
        return json.loads((D.WORK / draft_id / "catalog.json").read_text())["tables"]
    except OSError:
        return []


def _table_columns(draft_id: str, table: str) -> list[dict]:
    t = next((x for x in _catalog(draft_id) if x["name"].upper() == table.upper()), None)
    return t["columns"] if t else []


def _run(req_id: str) -> None:
    with D.db() as c:
        r = dict(c.execute("SELECT * FROM draft_requests WHERE id=?", (req_id,)).fetchone())
        d = dict(c.execute("SELECT provider FROM drafts WHERE id=?", (r["draft_id"],)).fetchone())
    try:
        el.set_provider(d["provider"])
        items = D.list_items(r["draft_id"], None, r["table_name"])
        short = {f"i{n + 1}": it for n, it in enumerate(items)}
        lines = []
        for sid, it in short.items():
            lines.append(json.dumps({"id": sid, "kind": it["kind"], "db": it["key"].lower(), "name": it["decided_name"] or it["name"],
                                     "label": it["decided_label"] if it["decided_label"] is not None else it["label"],
                                     "comment": it["decided_comment"] if it["decided_comment"] is not None else it["comment"],
                                     "status": it["status"], "notes": {k: v for k, v in it["detail"].items() if k in ("xsd", "where", "parent", "domain", "range", "technical")}},
                                    ensure_ascii=False))
        cols = [f"  - {c['name']} {c['type']}{' PK' if c['pk'] else ''}{' FK' if c['fk'] else ''}" for c in _table_columns(r["draft_id"], r["table_name"])]
        target = next((sid for sid, it in short.items() if it["id"] == r["item_id"]), None)
        cat = _catalog(r["draft_id"])
        others = [f"  - {t['name']} ({', '.join(t['pk']) or 'no primary key'})" for t in cat if t["name"].upper() != r["table_name"].upper()]
        prompt = "\n".join([f"Table: {r['table_name'].lower()}", "Columns:", *cols, "", "Other tables (primary key columns):", *others, "", "Draft items:", *lines, "",
                            f"The request is about item {target}." if target else "The request is about this table.",
                            "Steward's request:", r["comment"]])
        out = dmp.llm_json(SYSTEM, prompt)
        ops = _validate(out.get("operations") or [], short, cols_of=[c["name"] for c in _table_columns(r["draft_id"], r["table_name"])], catalog=cat)
        with D.db() as c:
            c.execute("UPDATE draft_requests SET status='proposed', message=?, operations=? WHERE id=?",
                      (out.get("message", ""), json.dumps(ops, ensure_ascii=False), req_id))
    except Exception:  # noqa: BLE001
        with D.db() as c:
            c.execute("UPDATE draft_requests SET status='failed', error=? WHERE id=?", (traceback.format_exc()[-2000:], req_id))


def _validate(ops: list[dict], short: dict, cols_of: list[str], catalog: list[dict] | None = None) -> list[dict]:
    """Keep only well-formed operations; attach the before state so the reviewer sees a diff."""
    good = []
    by_name = {t["name"].upper(): t for t in (catalog or [])}
    for op in ops:
        kind = op.get("op")
        if kind in ("update", "reject"):
            it = short.get(op.get("item"))
            if not it:
                continue
            before = {"name": it["decided_name"] or it["name"], "label": it["decided_label"] if it["decided_label"] is not None else it["label"],
                      "comment": it["decided_comment"] if it["decided_comment"] is not None else it["comment"]}
            entry = {"op": kind, "item_id": it["id"], "db": it["key"].lower(), "kind": it["kind"], "before": before}
            if kind == "update":
                after = {k: op[k] for k in ("name", "label", "comment") if op.get(k) not in (None, "")}
                if "name" in after and not NAME_RE.match(after["name"]):
                    continue
                if not after:
                    continue
                entry["after"] = {**before, **after}
            else:
                entry["reason"] = op.get("reason", "")
            good.append(entry)
        elif kind == "add_value_subclass":
            col = next((c for c in cols_of if c.lower() == str(op.get("column", "")).lower()), None)
            if not col or op.get("value") in (None, "") or not NAME_RE.match(str(op.get("class_name", ""))):
                continue
            good.append({"op": kind, "column": col, "value": str(op["value"]), "class_name": op["class_name"], "label": op.get("label", ""), "comment": op.get("comment", "")})
        elif kind == "add_relation":
            ref = by_name.get(str(op.get("ref_table", "")).upper())
            cols = [next((c for c in cols_of if c.lower() == str(x).lower()), None) for x in (op.get("columns") or [])]
            if not ref or not cols or None in cols or not NAME_RE.match(str(op.get("property_name", ""))):
                continue
            ref_cols_known = [c["name"] for c in ref["columns"]]
            rcols = [next((c for c in ref_cols_known if c.lower() == str(x).lower()), None) for x in (op.get("ref_columns") or ref["pk"])]
            if None in rcols or len(rcols) != len(cols):
                continue
            good.append({"op": kind, "columns": cols, "ref_table": ref["name"], "ref_columns": rcols, "property_name": op["property_name"],
                         "label": op.get("label", ""), "comment": op.get("comment", "")})
    return good


def _apply(req: dict) -> None:
    ops = json.loads(req["operations"] or "[]")
    table = req["table_name"]
    with D.db() as c:
        d = c.execute("SELECT model FROM drafts WHERE id=?", (req["draft_id"],)).fetchone()
        model = json.loads(d["model"])
        for op in ops:
            if op["op"] == "update":
                a = op["after"]
                c.execute("UPDATE draft_items SET status='approved', decided_name=?, decided_label=?, decided_comment=?, decided_at=? WHERE id=? AND draft_id=?",
                          (a["name"], a["label"], a["comment"], D.now(), op["item_id"], req["draft_id"]))
            elif op["op"] == "reject":
                c.execute("UPDATE draft_items SET status='rejected', decided_at=? WHERE id=? AND draft_id=?", (D.now(), op["item_id"], req["draft_id"]))
            elif op["op"] == "add_value_subclass":
                key = f"{table}={op['value']}"
                parent_name = model["classes"].get(table, {}).get("name", table)
                model["classes"][key] = {"name": op["class_name"], "label": op["label"], "comment": op["comment"], "parent": table,
                                         "kind": "value_subclass", "filter": [op["column"].upper(), op["value"]], "confidence": None}
                existing = c.execute("SELECT id FROM draft_items WHERE draft_id=? AND key=?", (req["draft_id"], key)).fetchone()
                detail = json.dumps({"kind": "value subclass", "where": f"{op['column'].upper()} = '{op['value']}'", "parent": parent_name, "source": "steward request"})
                if existing:
                    c.execute("UPDATE draft_items SET name=?, label=?, comment=?, detail=?, status='approved', decided_name=NULL, decided_label=NULL, decided_comment=NULL, decided_at=? WHERE id=?",
                              (op["class_name"], op["label"], op["comment"], detail, D.now(), existing["id"]))
                else:
                    c.execute("INSERT INTO draft_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (str(uuid.uuid4()), req["draft_id"], "subclass", table, key, json.dumps({"class": key}), op["class_name"], op["label"], op["comment"],
                               None, detail, "approved", None, None, None, D.now()))
            elif op["op"] == "add_relation":
                # an object property the database has no foreign key for; write_obda builds the target IRI from these columns
                ref_t = op["ref_table"].upper()
                key = f"{table}.{','.join(c.upper() for c in op['columns'])} -> {ref_t}"
                model["objprops"].append({"table": table, "cols": [c.upper() for c in op["columns"]], "name": op["property_name"], "label": op["label"],
                                          "comment": op["comment"], "domain_t": table, "range_t": ref_t, "join": None,
                                          "manual": {"ref_cols": [c.upper() for c in op["ref_columns"]]}, "confidence": None})
                idx = len(model["objprops"]) - 1
                dom_name = model["classes"].get(table, {}).get("name", table)
                rng_name = model["classes"].get(ref_t, {}).get("name", ref_t)
                detail = json.dumps({"domain": dom_name, "range": rng_name, "join": False, "source": "steward request (no foreign key in the database)",
                                     "on": " AND ".join(f"{table.lower()}.{a} = {op['ref_table']}.{b}" for a, b in zip(op["columns"], op["ref_columns"]))})
                existing = c.execute("SELECT id FROM draft_items WHERE draft_id=? AND key=?", (req["draft_id"], key)).fetchone()
                if existing:
                    c.execute("UPDATE draft_items SET ref=?, name=?, label=?, comment=?, detail=?, status='approved', decided_name=NULL, decided_label=NULL, decided_comment=NULL, decided_at=? WHERE id=?",
                              (json.dumps({"objprop": idx}), op["property_name"], op["label"], op["comment"], detail, D.now(), existing["id"]))
                else:
                    c.execute("INSERT INTO draft_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (str(uuid.uuid4()), req["draft_id"], "objprop", table, key, json.dumps({"objprop": idx}), op["property_name"], op["label"], op["comment"],
                               None, detail, "approved", None, None, None, D.now()))
        c.execute("UPDATE drafts SET model=? WHERE id=?", (json.dumps(model, ensure_ascii=False), req["draft_id"]))
        c.execute("UPDATE draft_requests SET status='applied', decided_at=? WHERE id=?", (D.now(), req["id"]))


# ──────────────────────────────── UI ────────────────────────────────


def _box_ctx(draft_id: str, table: str, item_id: str | None = None) -> dict:
    with D.db() as c:
        hist = [dict(r) for r in c.execute("SELECT * FROM draft_requests WHERE draft_id=? AND table_name=? ORDER BY created_at DESC LIMIT 8", (draft_id, table))]
        it = c.execute("SELECT id, key FROM draft_items WHERE id=?", (item_id,)).fetchone() if item_id else None
    for h in hist:
        h["ops"] = json.loads(h["operations"] or "[]")
    return {"draft_id": draft_id, "table": table, "target": dict(it) if it else None, "history": hist}


@router.get("/ui/drafts/{draft_id}/ask", response_class=HTMLResponse)
def ui_box(request: Request, draft_id: str, table: str, item: str | None = None):
    return templates.TemplateResponse(request, "ask_box.html", _box_ctx(draft_id, table, item))


@router.post("/ui/drafts/{draft_id}/ask", response_class=HTMLResponse)
def ui_ask(request: Request, draft_id: str, table: str = Form(...), comment: str = Form(...), item_id: str = Form("")):
    if not comment.strip():
        return templates.TemplateResponse(request, "ask_box.html", _box_ctx(draft_id, table, item_id or None))
    rid = str(uuid.uuid4())
    with D.db() as c:
        c.execute("INSERT INTO draft_requests (id,draft_id,table_name,item_id,comment,status,created_at) VALUES (?,?,?,?,?,?,?)",
                  (rid, draft_id, table, item_id or None, comment.strip(), "pending", D.now()))
    threading.Thread(target=_run, args=(rid,), daemon=True).start()
    return templates.TemplateResponse(request, "ask_box.html", _box_ctx(draft_id, table))


@router.get("/ui/requests/{req_id}", response_class=HTMLResponse)
def ui_request(request: Request, req_id: str):
    with D.db() as c:
        r = c.execute("SELECT * FROM draft_requests WHERE id=?", (req_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    h = dict(r)
    h["ops"] = json.loads(h["operations"] or "[]")
    return templates.TemplateResponse(request, "ask_request.html", {"h": h})


@router.post("/ui/requests/{req_id}/{action}", response_class=HTMLResponse)
def ui_decide(request: Request, req_id: str, action: str):
    with D.db() as c:
        r = c.execute("SELECT * FROM draft_requests WHERE id=?", (req_id,)).fetchone()
    if not r or r["status"] != "proposed" or action not in ("apply", "discard"):
        raise HTTPException(409, "nothing to decide")
    req = dict(r)
    if action == "apply":
        _apply(req)
    else:
        with D.db() as c:
            c.execute("UPDATE draft_requests SET status='discarded', decided_at=? WHERE id=?", (D.now(), req_id))
    # re-render the whole panel so rows, tabs and the class list reflect the change
    return templates.TemplateResponse(request, "draft_panel.html", D._panel_ctx(req["draft_id"], "all", req["table_name"]))
