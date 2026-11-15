"""Build the context an LLM needs to answer questions over a database using an approved Tabulary draft.

The output of describe() is plain Markdown: business classes with the SQL table that realises each one,
business attributes with their columns, and relations with their JOIN conditions. This is the "ontology as
context" path: the LLM thinks in business terms and writes SQL against the names given here.

Export rule is the same as for v1.ttl / v1.obda (server.drafts._final_model): structure taken from the database is
included unless rejected (unreviewed items are marked), LLM-added structure (value subclasses, superclasses) only
when approved.
"""
from __future__ import annotations

import re
from collections import defaultdict

from server import drafts as D

SHARED = D.SHARED


def latest_done_draft_id() -> str | None:
    with D.db() as c:
        r = c.execute("SELECT id FROM drafts WHERE status='done' ORDER BY created_at DESC LIMIT 1").fetchone()
    return r["id"] if r else None


def draft_info(draft_id: str) -> dict:
    with D.db() as c:
        r = c.execute("SELECT id, schema_name, conninfo, connection_id, domain, created_at FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if not r:
        raise ValueError(f"draft not found: {draft_id}")
    return dict(r)


def load(draft_id: str) -> tuple[dict, dict]:
    """(final model, direct mapping) for a finished draft."""
    return D._final_model(draft_id)


def _pk_cols(dm: dict, tu: str) -> list[str]:
    """Columns that identify a row, in the order Ontop used them for the IRI template (the primary key)."""
    return [c for c in re.findall(r"\{([^}]+)\}", dm[tu]["subject"])]


def _tbl(dm: dict, tu: str) -> str:
    """Unquoted table name for prose and JOIN conditions (the last component of the SQL name)."""
    return dm[tu]["sql_name"].rsplit(".", 1)[-1].strip('"') if tu in dm else tu.lower()


def _colname(dm: dict, tu: str, col: str) -> str:
    c = dm.get(tu, {}).get("cols", {}).get(col.upper())
    return c["name"] if c else col


def _xsd_short(xsd: str | None) -> str:
    return (xsd or "").split(":")[-1].replace("#", "") or "text"


def describe(model: dict, dm: dict, info: dict, detail: str = "full") -> str:
    classes: dict[str, dict] = model["classes"]
    subtype_of: dict[str, dict] = model.get("subtype_of", {})
    dt_by_class: dict[str, list[dict]] = defaultdict(list)
    for p in model["dtprops"]:
        dt_by_class[p.get("domain_class") or p["table"]].append(p)
    rel_by_class: dict[str, list[dict]] = defaultdict(list)
    for p in model["objprops"]:
        rel_by_class[p["domain_t"]].append(p)
    # value subclasses hang under their parent table
    subs_by_parent: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for k, c in classes.items():
        if c.get("filter"):
            subs_by_parent[c["parent"]].append((k, c))

    def cname(key: str) -> str:
        return classes[key]["name"] if key in classes else key

    def unrev(x: dict) -> str:
        return " (unreviewed)" if x.get("unreviewed") else ""

    schema = info.get("schema_name") or "public"
    L = [f"# Business vocabulary: {info.get('domain') or schema} (Tabulary draft {info['id'][:8]})", "",
         f"PostgreSQL schema \"{schema}\". Use the SQL table and column names given in parentheses in queries; "
         "the search_path is set to this schema so unqualified table names work. Business names (CamelCase) are "
         "for understanding, not SQL identifiers.", ""]
    if detail == "summary":
        L.append("## Classes")
        for k, c in classes.items():
            if c.get("filter"):
                col, val = c["filter"]
                where = f"{_tbl(dm, c['parent'])}.{_colname(dm, c['parent'], col)} = '{val}'"
                L.append(f"- {c['name']}: {c.get('comment') or ''} (rows of {cname(c['parent'])} where {where}){unrev(c)}")
            elif k.startswith(SHARED):
                L.append(f"- {c['name']} (abstract, no table): {c.get('comment') or ''}{unrev(c)}")
            else:
                L.append(f"- {c['name']} (table {_tbl(dm, k)}): {c.get('comment') or ''}{unrev(c)}")
        return "\n".join(L) + "\n"

    L.append("## Classes")
    # abstract superclasses first, then tables
    for k, c in classes.items():
        if not k.startswith(SHARED):
            continue
        members = [cname(m) for m, mc in classes.items() if k in (mc.get("extra_parents") or [])]
        L += ["", f"### {c['name']} (abstract, no table of its own){unrev(c)}", c.get("comment") or "",
              f"Members: {', '.join(members)}" if members else ""]
        shared = {p["name"]: p for p in dt_by_class.get(k, [])}  # one line per shared property, not one per member table
        if shared:
            L.append("Shared attributes (each member table has the column):")
            for p in shared.values():
                L.append(f"- {p['name']} ({p['col_sql']}, {_xsd_short(p.get('xsd'))}): {p.get('comment') or ''}{unrev(p)}")
    for k, c in classes.items():
        if k.startswith(SHARED) or c.get("filter") or k not in dm:
            continue
        t = _tbl(dm, k)
        L += ["", f"### {c['name']} (table {t}){unrev(c)}", c.get("comment") or ""]
        pk = _pk_cols(dm, k)
        if pk:
            L.append(f"Identity: {', '.join(f'{t}.{x}' for x in pk)}")
        parents = []
        st = subtype_of.get(k)
        if c.get("parent") and st and st["parent"] in classes:
            pt = _tbl(dm, st["parent"])
            cond = " AND ".join(f"{t}.{_colname(dm, k, a)} = {pt}.{_colname(dm, st['parent'], b)}" for a, b in zip(st["cols"], st["ref_cols"]))
            parents.append(f"{cname(st['parent'])} (same individual; join {cond})")
        elif c.get("parent") and c["parent"] in classes:
            parents.append(cname(c["parent"]))
        for ep in c.get("extra_parents") or []:
            parents.append(cname(ep))
        if parents:
            L.append("Subclass of: " + "; ".join(parents))
        business = [p for p in dt_by_class.get(k, []) if not p.get("technical")]
        technical = [p for p in dt_by_class.get(k, []) if p.get("technical")]
        if business:
            L.append("Attributes:")
            for p in business:
                L.append(f"- {p['name']} ({t}.{p['col_sql']}, {_xsd_short(p.get('xsd'))}): {p.get('comment') or ''}{unrev(p)}")
        if technical:  # keys and foreign-key columns: needed for joins, not business attributes
            L.append("Key columns: " + ", ".join(f"{t}.{p['col_sql']}" for p in technical))
        rels = rel_by_class.get(k, [])
        if rels:
            L.append("Relations:")
            for p in rels:
                L.append(f"- {p['name']} -> {cname(p['range_t'])}: {_join_text(dm, p)}; {p.get('comment') or ''}{unrev(p)}")
        if subs_by_parent.get(k):
            L.append("Subclasses by value:")
            for sk, sc in subs_by_parent[k]:
                col, val = sc["filter"]
                L.append(f"- {sc['name']}: rows where {t}.{_colname(dm, k, col)} = '{val}'; {sc.get('comment') or ''}{unrev(sc)}")
    return "\n".join(x for x in L if x is not None) + "\n"


def _join_text(dm: dict, p: dict) -> str:
    """JOIN condition in SQL terms for an object property."""
    if p.get("join"):
        jt = _tbl(dm, p["table"])
        parts = []
        for side in ("dom_fk", "rng_fk"):
            fk = p["join"][side]
            rt = fk["ref_table"].rsplit(".", 1)[-1].strip('"')
            parts.append(" AND ".join(f"{jt}.{a} = {rt}.{b}" for a, b in zip(fk["columns"], fk["ref_columns"])))
        return f"via table {jt} ({parts[0]}; {parts[1]})"
    t, rt = _tbl(dm, p["table"]), _tbl(dm, p["range_t"])
    ref_pk = _pk_cols(dm, p["range_t"])
    cols = [_colname(dm, p["table"], c) for c in p["cols"]]
    return " AND ".join(f"{t}.{a} = {rt}.{b}" for a, b in zip(cols, ref_pk)) if ref_pk else f"{t}.{', '.join(cols)}"


def known_tables(dm: dict) -> dict[str, str]:
    """unquoted table name -> quoted schema-qualified SQL name"""
    return {_tbl(dm, tu): dm[tu]["sql_name"] for tu in dm}


def known_columns(dm: dict, tu: str) -> list[str]:
    return [c["name"] for c in dm[tu]["cols"].values()]
