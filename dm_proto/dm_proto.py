#!/usr/bin/env python3
"""No-target-ontology path: Ontop bootstrap output -> LLM pruning + naming -> the company's own ontology v1.

Inputs:
  ../bench/insurance/boot.obda  (Ontop bootstrap = Direct Mapping: every table, column and FK mechanically turned into vocabulary)
  ../bench/insurance/catalog.json  (structure facts and column profiles from scan/extract_pg.py; --catalog yaml falls back to the el_proto benchmark YAML)
Stages:
  A. read the Direct Mapping (Ontop's output is the skeleton; we do not write our own Direct Mapping)
  B. one LLM call per table: table kind (entity/subtype/join/code_list), class name + description, keep/drop + property name + description per column, relation name per FK
  C. one LLM call to harmonize naming across the namespace (optional, skip with --no-harmonize)
  D. output: out/v1.ttl (with rdfs:label/comment), out/v1.obda (boot.obda rewritten to new IRIs, dropped columns removed, join tables as one relation), out/review.json
  E. scoring: compare the structure (kept / dropped) with gold_r2rml.yaml; name agreement with the gold is reported for reference
Serial. LLM responses are cached by input hash (shares el_proto/cache).
Usage: python3 dm_proto.py --provider deepseek [--stats-level counts|values] [--domain "P&C insurance"] [--no-harmonize]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "el_proto"))
import el_proto as el  # noqa: E402  (load_schema, words, set_provider, _cache_path, llm_client, LLM_MODEL)

BOOT_OBDA = ROOT / "bench" / "insurance" / "boot.obda"  # benchmark input (insurance demo on Postgres)
GOLD = ROOT / "el_proto" / "gold_r2rml.yaml"
OUT = HERE / "out"  # default output directory
BOOT_PREFIX = "urn:tabulary:boot:/"

# ──────────────────────────────── 0. Input (catalog JSON or benchmark YAML) ────────────────────────────────


def derive_structure(tables: list[dict]) -> None:
    """Structure derivable deterministically: parent/child (PK is also an FK = subtype), join table (PK made only of FK columns)"""
    by_name = {t["name"].upper(): t for t in tables}
    for t in tables:
        pk = sorted(t["pk"])
        t["parent"] = next((fk["ref_table"] for fk in t["fks"] if sorted(fk["columns"]) == pk and fk["ref_table"].upper() != t["name"].upper()), None)
        t["children"] = []
        fk_cols = {c for fk in t["fks"] for c in fk["columns"]}
        t["join_table"] = len(t["fks"]) >= 2 and set(t["pk"]) <= fk_cols and all(c["pk"] or c["fk"] for c in t["columns"])
    for t in tables:
        if t["parent"] and t["parent"].upper() in by_name:
            by_name[t["parent"].upper()]["children"].append(t["name"])


def load_catalog(path: Path) -> tuple[list[dict], dict]:
    """extract_pg.py catalog -> (tables, stats). tables has the same shape as el.load_schema(); stats is {TABLE: {rows, columns: {COL: {...}}}}"""
    cat = json.loads(Path(path).read_text())
    tables, stats = [], {}
    for t in cat["tables"]:
        tables.append({
            "name": t["name"], "pk": sorted(t["pk"]),
            "columns": [{"name": c["name"], "type": c["type"], "pk": c["pk"], "fk": c["fk"], "nullable": c["nullable"]} for c in t["columns"]],
            "fks": [{"columns": fk["columns"], "ref_table": fk["ref_table"], "ref_columns": fk["ref_columns"]} for fk in t["fks"]],
        })
        stats[t["name"].upper()] = {"rows": t.get("rows", 0), "columns": {c["name"].upper(): c["stats"] for c in t["columns"] if c.get("stats")}}
    derive_structure(tables)
    return tables, stats


# ──────────────────────────────── A. Read the Direct Mapping ────────────────────────────────


def _in_schema(source: str, schema: str | None) -> bool:
    """Ontop bootstraps every schema it can see; keep only the mappings that read from the schema we extracted."""
    if not schema:
        return True
    qualified = re.search(r'FROM\s+"([^"]+)"\."', source)
    return qualified.group(1) == schema if qualified else schema == "public"


def parse_obda(path: Path, schema: str | None = None) -> list[dict]:
    """boot.obda -> [{id, subject, triples:[(pred, obj)], source}]. subject is an IRI template (without <>) or a blank-node
    template ('_:...', used by Ontop for tables without a primary key). pred is 'a' or an IRI; obj is an IRI template or '{COL}^^xsd:t'."""
    maps, cur = [], {}
    for line in path.read_text().splitlines():
        if line.startswith("mappingId"):
            cur = {"id": line.split("\t", 1)[1].strip()}
        elif line.startswith("target"):
            tgt = line.split("\t", 1)[1].strip().rstrip(" .")
            m = re.match(r"(?:<([^>]+)>|(_:\S+))\s+(.*)$", tgt)
            cur["subject"] = m.group(1) or m.group(2)
            triples = []
            for po in re.split(r"\s;\s", m.group(3)):
                p, o = po.split(" ", 1)
                triples.append((p if p == "a" else p.strip("<>"), o.strip()))
            cur["triples"] = triples
        elif line.startswith("source"):
            cur["source"] = line.split("\t", 1)[1].strip()
            if _in_schema(cur["source"], schema):
                maps.append(cur)
    return maps


def table_of(iri: str) -> str:
    """urn:tabulary:boot:/CLAIM/... or urn:tabulary:boot:/CLAIM#COL → CLAIM"""
    rest = iri[len(BOOT_PREFIX):]
    return re.split(r"[/#]", rest, 1)[0]


def direct_mapping(maps: list[dict]) -> dict[str, dict]:
    """TABLE (upper case) -> {subject, class, sql_name, cols:{COL: {xsd, name}}, fks:[{cols:[COL..], ref:TABLE, map, pred}]}
    Identifier case differs by database (H2 upper, Postgres lower), so matching uses upper-case keys while SQL and templates keep the original spelling."""
    dm: dict[str, dict] = defaultdict(lambda: {"subject": None, "class": None, "sql_name": None, "cols": {}, "fks": []})
    for m in maps:
        p0, o0 = m["triples"][0]
        t = table_of(o0.strip("<>") if p0 == "a" else p0).upper()
        for p, o in m["triples"]:
            if p == "a":
                dm[t]["subject"] = m["subject"]
                dm[t]["class"] = o.strip("<>")
                mt = re.search(r'FROM\s+((?:"[^"]+"\.)?"[^"]+"|\S+)', m["source"])  # keep the quoted, schema-qualified name as is
                dm[t]["sql_name"] = mt.group(1) if mt else '"' + t.lower() + '"'
            elif o.startswith("{"):
                col, _, xsd = re.match(r"\{([^}]+)\}(\^\^(\S+))?", o).groups()
                dm[t]["cols"][col.upper()] = {"xsd": xsd, "name": col}
            else:  # ref-: object property to another table
                cols = [c.upper() for c in p.rsplit("#ref-", 1)[1].split(";")]
                dm[t]["fks"].append({"cols": cols, "ref": table_of(o.strip("<>")).upper(), "map": m, "pred": p})
    return dm


# ──────────────────────────────── B. Per-table LLM judgment ────────────────────────────────

SYSTEM = """You design a company's first business ontology (OWL) from a relational schema. There is NO existing
ontology to map to: you decide which tables and columns become classes and properties, and you name and describe them.
A mechanical direct mapping already turned every table into a class, every column into a datatype property and every
foreign key into an object property. Your job is to prune it and give it business names.

Rules:
- kind: "entity" (a business thing), "subtype" (primary key is also a foreign key to a parent table; becomes a subclass,
  inherits the parent's properties), "join" (primary key consists only of foreign keys; NOT a class, it realises ONE
  object property between the two referenced tables), "code_list" (a lookup/code table; keep as a class).
- Identifier columns (surrogate keys, foreign key columns) are NOT datatype properties: they are used to build IRIs or
  are covered by object properties. Set keep=false for them. Keep business attributes (numbers shown to users, dates,
  amounts, descriptions, status/type codes).
- Names: class_name in UpperCamelCase, property_name in lowerCamelCase, concise business English, no table prefixes
  unless needed to disambiguate (e.g. two tables both have Effective_Date: name them policyEffectiveDate and
  policyCoverageEffectiveDate). Expand abbreviations. Amount columns on a subtype should carry the subtype's meaning
  in the name only if the column lives on the subtype table itself.
- Object properties: domain is this table's class, range is the referenced table's class; name them as a verb phrase
  (hasCatastrophe, involvesInsurableObject). A subtype's foreign key to its parent gets no property (subclass covers it).
  A foreign key that points to the table itself on its own primary key is technical: no property.
- For a join table, fill "join_relation": which foreign key is the domain side, which is the range side, the property name.
- VALUE-DEPENDENT SUBCLASSES: when a low-cardinality code/type/role column decides WHAT the row is (a party-role code
  PH/AG, a type code), propose one subclass per value in "subclasses_by_value" (class_name, the column and the value that
  selects it). Only do this when the column profile shows the actual values; otherwise leave the list empty.
- description: one sentence in plain business English a data steward would accept. label: short human label.
- confidence: 0.0-1.0 how sure you are about the structural decision (kind / keep), not about the wording.

Output raw JSON only:
{"table": {"kind": "entity|subtype|join|code_list", "class_name": "<UpperCamel or null>", "label": "...", "description": "...", "confidence": 0.0},
 "columns": [{"name": "...", "keep": true, "property_name": "<lowerCamel or null>", "label": "...", "description": "...", "confidence": 0.0}],
 "foreign_keys": [{"columns": ["..."], "property_name": "<lowerCamel or null>", "label": "...", "description": "...", "confidence": 0.0}],
 "join_relation": {"property_name": "...", "domain_columns": ["..."], "range_columns": ["..."], "label": "...", "description": "..."} | null,
 "subclasses_by_value": [{"column": "...", "value": "...", "class_name": "<UpperCamel>", "label": "...", "description": "...", "confidence": 0.0}]}"""


def llm_json(system: str, prompt: str) -> dict:
    p = el._cache_path("llm", el.LLM_MODEL + "\n" + system + "\n" + prompt)
    if p.exists():
        return json.loads(p.read_text())
    resp = el.llm_client.chat.completions.create(
        model=el.LLM_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    )
    out = json.loads(resp.choices[0].message.content or "{}")
    out["_usage"] = {"in": resp.usage.prompt_tokens, "out": resp.usage.completion_tokens}
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def col_profile(tstats: dict | None, col: str, level: str) -> str:
    """Column profile -> one prompt line. level: none (omit) / counts (counts only, for an external LLM) / values (also top values, for an in-house LLM)"""
    if level == "none" or not tstats or col.upper() not in tstats["columns"]:
        return ""
    st = tstats["columns"][col.upper()]
    out = f"  | rows {tstats['rows']}, non-null {st['non_null']}, distinct {st['distinct']}"
    if level == "values" and st["top"]:
        out += ", top: " + ", ".join(f"{v}({n})" for v, n in st["top"]) + (f", range {st['min']}..{st['max']}" if st["distinct"] > len(st["top"]) else "")
    return out


def propose(table: dict, all_tables: list[str], domain: str | None, extra_fact: str | None = None, tstats: dict | None = None, level: str = "none") -> dict:
    cols = "\n".join(
        f"  - {c['name']} {c['type']}{' PK' if c['pk'] else ''}{' FK' if c['fk'] else ''}{' NOT NULL' if not c['nullable'] else ''}" + col_profile(tstats, c["name"], level)
        for c in table["columns"]
    )
    fks = "\n".join(f"  - ({', '.join(fk['columns'])}) -> {fk['ref_table']}({', '.join(fk['ref_columns'])})" for fk in table["fks"]) or "  (none)"
    facts = []
    if table["parent"]:
        facts.append(f"SUBTYPE of {table['parent']} (primary key is a foreign key to it)")
    if table["children"]:
        facts.append(f"SUPERTYPE with subtype tables: {', '.join(table['children'])}")
    if table["join_table"]:
        facts.append("JOIN table (primary key consists only of foreign keys)")
    if extra_fact:
        facts.append(extra_fact)
    user = [
        f"Domain: {domain}" if domain else "Domain: (infer from the schema)",
        "All tables in the schema: " + ", ".join(all_tables),
        "",
        f"Table: {table['name']}  (words: {el.words(table['name'])})",
        "Structure facts: " + ("; ".join(facts) if facts else "ordinary entity table"),
        "Columns" + (" (with profile from the data)" if level != "none" and tstats else "") + ":", cols, "Foreign keys:", fks,
    ]
    return llm_json(SYSTEM, "\n".join(user))


# ──────────────────────────────── C. Harmonize ────────────────────────────────

HARMONIZE = """You review the vocabulary of a newly drafted business ontology for consistency. You receive every class and
property name with where it came from. Fix ONLY inconsistencies: same concept named differently, different concepts
named the same, inconsistent prefixes/suffixes (Id vs Identifier, Date vs Dt), wrong case style. Do not rename things
that are already fine. One property name used from several tables with the SAME meaning (e.g. hasGeographicLocation on
both Policy and PolicyAmount) is correct OWL (one property, several domains): never rename those, never add table
prefixes to object properties. Rename a shared name only when the two usages mean genuinely different things.
Output raw JSON: {"renames": [{"from": "...", "to": "...", "why": "..."}]}"""


def harmonize(vocab: list[tuple[str, str, str]]) -> dict[str, str]:
    lines = [f"  - {kind} {name}  (from {origin})" for kind, name, origin in vocab]
    out = llm_json(HARMONIZE, "Vocabulary:\n" + "\n".join(lines))
    return {r["from"]: r["to"] for r in out.get("renames", []) if r.get("from") and r.get("to") and r["from"] != r["to"]}


SUPERCLASS = """You review a draft business ontology built bottom-up from a database. Some datatype properties are used by
several classes (e.g. firstName on Student and Academic). For each group of classes that share properties, decide whether
they are kinds of one more general business concept (Student and Academic are both a Person). If so, propose that
superclass: UpperCamelCase name, short label, one-sentence description. If the shared names are a coincidence (e.g. a
"title" of a course and a "title" of a person), do not propose a superclass for that group.
Also write ONE neutral description for every shared property that fits all classes using it (no class-specific wording).
Output raw JSON only:
{"superclasses": [{"name": "...", "label": "...", "description": "...", "members": ["<class name>", ...], "properties": ["<property>", ...], "confidence": 0.0}],
 "property_descriptions": {"<property>": "..."}}"""


def propose_superclasses(model: dict) -> dict:
    """Group datatype properties used by 2+ classes, ask the LLM for superclasses, and neutralise their descriptions.
    Proposals are stored in model["superclasses"] (members as table keys); they only enter the output once a reviewer approves them."""
    cls = model["classes"]
    users: dict[str, list[str]] = defaultdict(list)
    for p in model["dtprops"]:
        if p["table"] not in users[p["name"]]:
            users[p["name"]].append(p["table"])
    shared = {n: ts for n, ts in users.items() if len(ts) > 1}
    model["superclasses"] = []
    if not shared:
        return model
    lines = [f"  - {n}: used by " + ", ".join(cls[t]["name"] for t in ts) for n, ts in sorted(shared.items())]
    classes = "\n".join(f"  - {cls[t]['name']}: {cls[t]['comment']}" for t in dict.fromkeys(t for ts in shared.values() for t in ts))
    out = llm_json(SUPERCLASS, "Classes:\n" + classes + "\n\nShared datatype properties:\n" + "\n".join(lines))
    by_name = {c["name"]: tu for tu, c in cls.items()}
    for sc in out.get("superclasses") or []:
        members = [by_name[m] for m in sc.get("members") or [] if m in by_name]
        if len(members) < 2 or not sc.get("name") or sc["name"] in by_name:
            continue
        model["superclasses"].append({"name": sc["name"], "label": sc.get("label", ""), "comment": sc.get("description", ""),
                                      "members": members, "properties": [x for x in sc.get("properties") or [] if x in shared],
                                      "confidence": sc.get("confidence")})
    for name, desc in (out.get("property_descriptions") or {}).items():
        if name in shared and desc:
            for p in model["dtprops"]:
                if p["name"] == name:
                    p["comment"] = desc
    return model


# ──────────────────────────────── D. Output ────────────────────────────────


def upper_camel(s: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[^A-Za-z0-9]+", s) if w)


def build_model(tables: list[dict], props: dict[str, dict], dm: dict[str, dict], ns: str) -> dict:
    """LLM proposals + structure facts -> output model {classes, dtprops, objprops, pruned}"""
    by_name = {t["name"]: t for t in tables}
    classes: dict[str, dict] = {}   # TABLE(upper) → {name, label, comment, parent}
    dtprops: list[dict] = []        # {table, col, name, label, comment, xsd}
    objprops: list[dict] = []       # {table, cols, name, label, comment, domain_t, range_t, join}
    pruned: list[str] = []

    def cls_for(tname_upper: str) -> str | None:
        return classes.get(tname_upper, {}).get("name")

    # Referenced tables outside the schema (key-only stubs in the DDL) are named mechanically
    for t_up in dm:
        if t_up not in {t["name"].upper() for t in tables}:
            classes[t_up] = {"name": upper_camel(t_up.lower()), "label": el.words(t_up.lower()).title(), "comment": "Referenced entity (definition out of scope of this extract).", "parent": None, "stub": True}

    for t in tables:
        p = props[t["name"]]
        tu = t["name"].upper()
        kind = p["table"].get("kind", "entity")
        if t["join_table"] and kind != "join":
            kind = "join"  # deterministic facts win
        if kind == "join" and not t["join_table"]:
            kind = "entity"  # an association table with its own attributes (e.g. role + validity period) stays a class; the LLM's join verdict is overruled
        if t["parent"] and kind != "join":
            kind = "subtype"
        if kind != "join":
            classes[tu] = {
                "name": p["table"].get("class_name") or upper_camel(t["name"]),
                "label": p["table"].get("label") or el.words(t["name"]),
                "comment": p["table"].get("description", ""),
                "parent": t["parent"].upper() if t["parent"] else None,
                "kind": kind,
                "confidence": p["table"].get("confidence"),
            }
            colnames = {c["name"].upper() for c in t["columns"]}
            for sc in p.get("subclasses_by_value") or []:
                if not sc.get("class_name") or str(sc.get("column", "")).upper() not in colnames or sc.get("value") in (None, ""):
                    continue
                classes[f"{tu}={sc['value']}"] = {"name": sc["class_name"], "label": sc.get("label", ""), "comment": sc.get("description", ""), "parent": tu,
                                                   "kind": "value_subclass", "filter": (str(sc["column"]).upper(), str(sc["value"])), "confidence": sc.get("confidence")}
        else:
            pruned.append(f"class {t['name']} (join table)")
        colinfo = {c["name"].upper(): c for c in t["columns"]}
        for c in p.get("columns", []):
            cu = c["name"].upper()
            if cu not in colinfo:
                continue
            if kind == "join" or not c.get("keep") or not c.get("property_name"):
                pruned.append(f"column {t['name']}.{c['name']}")
                continue
            dcol = dm[tu]["cols"].get(cu) or {"xsd": None, "name": c["name"]}
            dtprops.append({"table": tu, "col": cu, "col_sql": dcol["name"], "name": c["property_name"], "label": c.get("label", ""), "comment": c.get("description", ""),
                            "xsd": dcol["xsd"], "confidence": c.get("confidence")})
        fk_props = {",".join(x.upper() for x in f["columns"]): f for f in p.get("foreign_keys", [])}
        for fk in t["fks"]:
            key = ",".join(x.upper() for x in fk["columns"])
            ru = fk["ref_table"].upper()
            self_pk = ru == tu and sorted(fk["columns"]) == sorted(t["pk"])
            parent_link = t["parent"] and ru == t["parent"].upper() and sorted(fk["columns"]) == sorted(t["pk"])
            if kind == "join" or self_pk or parent_link:
                pruned.append(f"fk {t['name']}.{key} -> {fk['ref_table']}" + (" (join)" if kind == "join" else " (subtype/self link)"))
                continue
            f = fk_props.get(key, {})
            if not f.get("property_name"):
                pruned.append(f"fk {t['name']}.{key} -> {fk['ref_table']} (LLM: none)")
                continue
            objprops.append({"table": tu, "cols": [x.upper() for x in fk["columns"]], "name": f["property_name"], "label": f.get("label", ""),
                             "comment": f.get("description", ""), "domain_t": tu, "range_t": ru, "join": None, "confidence": f.get("confidence")})
        if kind == "join":
            jr = p.get("join_relation") or {}
            fks2 = t["fks"]
            dom_cols = [x.upper() for x in (jr.get("domain_columns") or fks2[0]["columns"])]
            dom_fk = next((f for f in fks2 if sorted(x.upper() for x in f["columns"]) == sorted(dom_cols)), fks2[0])
            rng_fk = next((f for f in fks2 if f is not dom_fk), fks2[-1])
            objprops.append({"table": tu, "cols": [x.upper() for x in dom_fk["columns"]], "name": jr.get("property_name") or f"relatedTo{upper_camel(rng_fk['ref_table'])}",
                             "label": jr.get("label", ""), "comment": jr.get("description", ""), "domain_t": dom_fk["ref_table"].upper(), "range_t": rng_fk["ref_table"].upper(),
                             "join": {"dom_fk": dom_fk, "rng_fk": rng_fk}, "confidence": p["table"].get("confidence")})
    subtype_of = {}
    for t in tables:
        if t["parent"]:
            fk = next((f for f in t["fks"] if sorted(f["columns"]) == sorted(t["pk"]) and f["ref_table"].upper() == t["parent"].upper()), None)
            if fk:
                subtype_of[t["name"].upper()] = {"parent": t["parent"].upper(), "cols": [c.upper() for c in fk["columns"]], "ref_cols": [c.upper() for c in fk["ref_columns"]]}
    return {"classes": classes, "dtprops": dtprops, "objprops": objprops, "pruned": pruned, "ns": ns, "subtype_of": subtype_of}


def apply_renames(model: dict, renames: dict[str, str]) -> None:
    for c in model["classes"].values():
        c["name"] = renames.get(c["name"], c["name"])
    for p in model["dtprops"] + model["objprops"]:
        p["name"] = renames.get(p["name"], p["name"])


XSD_FALLBACK = "xsd:string"


def write_ttl(model: dict, path: Path) -> None:
    ns = model["ns"]
    L = [f"@prefix : <{ns}> .", "@prefix owl: <http://www.w3.org/2002/07/owl#> .", "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
         "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .", "@prefix tabulary: <urn:tabulary:vocab:> .", "",
         f"<{ns.rstrip('#/')}> a owl:Ontology ; rdfs:comment \"Draft v1 generated from the relational schema; pending steward review.\" .",
         'tabulary:reviewStatus a owl:AnnotationProperty ; rdfs:comment "Set to \\"unreviewed\\" on terms nobody has confirmed yet." .', ""]
    unrev = lambda *es: ' ; tabulary:reviewStatus "unreviewed"' if any(e.get("unreviewed") for e in es) else ""  # noqa: E731
    q = lambda s: json.dumps(s or "", ensure_ascii=False)  # noqa: E731
    cls = model["classes"]
    for tu, c in cls.items():
        parents = [x for x in ([c.get("parent")] + list(c.get("extra_parents") or [])) if x and x in cls]
        L.append(f":{c['name']} a owl:Class ; rdfs:label {q(c['label'])} ; rdfs:comment {q(c['comment'])}" + "".join(f" ; rdfs:subClassOf :{cls[x]['name']}" for x in parents) + unrev(c) + " .")
    L.append("")
    # When one property name is used from several tables, do not write a domain (range). Writing just one would make individuals of the other
    # table be inferred as instances of that domain (Ontop uses rdfs:domain for reasoning). Keep a note instead
    def dom_rng(entries: list[dict], key: str) -> tuple[str | None, list[str]]:
        names = list(dict.fromkeys(cls[e.get("domain_class", e[key]) if key in ("table", "domain_t") else e[key]]["name"] for e in entries))
        return (names[0] if len(names) == 1 else None), names
    seen: dict[str, list[dict]] = defaultdict(list)
    for p in model["dtprops"]:
        seen[p["name"]].append(p)
    for name, ps in seen.items():
        p = ps[0]
        d, ds = dom_rng(ps, "table")
        rngs = list(dict.fromkeys(x["xsd"] or XSD_FALLBACK for x in ps))
        L.append(f":{name} a owl:DatatypeProperty ; rdfs:label {q(p['label'])} ; rdfs:comment {q(p['comment'])}" + (f" ; rdfs:domain :{d}" if d else "") + (f" ; rdfs:range {rngs[0]}" if len(rngs) == 1 else "") + unrev(*ps) + " ." + (f"  # used on: {', '.join(ds)}" if not d else ""))
    L.append("")
    seen_o: dict[str, list[dict]] = defaultdict(list)
    for p in model["objprops"]:
        seen_o[p["name"]].append(p)
    for name, ps in seen_o.items():
        p = ps[0]
        d, ds = dom_rng(ps, "domain_t")
        r, rs = dom_rng(ps, "range_t")
        L.append(f":{name} a owl:ObjectProperty ; rdfs:label {q(p['label'])} ; rdfs:comment {q(p['comment'])}" + (f" ; rdfs:domain :{d}" if d else "") + (f" ; rdfs:range :{r}" if r else "") + unrev(*ps) + " ." + (f"  # domains: {', '.join(ds)}; ranges: {', '.join(rs)}" if not (d and r) else ""))
    path.write_text("\n".join(L) + "\n")


def write_obda(model: dict, dm: dict[str, dict], path: Path, data_ns: str) -> None:
    ns = model["ns"]
    cls = model["classes"]
    subtype_of = model.get("subtype_of", {})

    def subj(tu: str, ph: dict[str, str] | None = None) -> str:
        """Subject IRI template for table tu. ph maps COLUMN (upper case) -> placeholder text ("{alias}").
        A subtype table describes the same individual as its parent, so the parent's template is used with this table's FK columns plugged in (recursive)."""
        if ph is None:
            ph = {cu: "{" + c["name"] + "}" for cu, c in dm[tu]["cols"].items()}
        st = subtype_of.get(tu)
        if st and st["parent"] in dm and all(c in ph for c in st["cols"]):
            return subj(st["parent"], {rc: ph[c] for c, rc in zip(st["cols"], st["ref_cols"])})
        s = dm[tu]["subject"].replace(BOOT_PREFIX, data_ns)
        for cu, text in ph.items():
            native = (dm[tu]["cols"].get(cu) or {"name": cu})["name"]
            s = re.sub(r"\{" + re.escape(native) + r"\}", text, s, flags=re.IGNORECASE)
        return s

    def term(t: str) -> str:
        return t if t.startswith("_:") else f"<{t}>"

    def ph_of(template: str, tu: str) -> dict[str, str]:
        """Placeholders of a subject template from a join mapping -> {COL: '{alias}'}.
        IRI templates name their columns ('.../col={alias};col2={alias2}'); blank-node templates do not
        ('_:b{alias1}/{alias2}'), so those are matched by position against the table's own subject template."""
        named = {m.group(1).upper(): "{" + m.group(2) + "}" for m in re.finditer(r"([^/;={}]+)=\{([^}]+)\}", template)}
        if named:
            return named
        own = re.findall(r"\{([^}]+)\}", dm[tu]["subject"])
        return {c.upper(): "{" + a + "}" for c, a in zip(own, re.findall(r"\{([^}]+)\}", template))}
    L = ["[PrefixDeclaration]", f":\t\t{ns}", "xsd:\t\thttp://www.w3.org/2001/XMLSchema#", "", "[MappingDeclaration] @collection [["]
    n = 0
    dt_by_table: dict[str, list[dict]] = defaultdict(list)
    for p in model["dtprops"]:
        dt_by_table[p["table"]].append(p)
    for tu in dm:
        if tu not in cls:
            continue
        n += 1
        parts = [f"a :{cls[tu]['name']}"]
        for p in dt_by_table[tu]:
            parts.append(f":{p['name']} {{{p['col_sql']}}}" + (f"^^{p['xsd']}" if p["xsd"] else ""))
        L += [f"mappingId\tM{n}", f"target\t\t{term(subj(tu))} " + " ; ".join(parts) + " .", f"source\t\tSELECT * FROM {dm[tu]['sql_name']}", ""]
    for key, c in cls.items():
        if c.get("filter"):
            n += 1
            col, val = c["filter"]
            colsql = (dm[c["parent"]]["cols"].get(col) or {"name": col})["name"]
            L += [f"mappingId\tM{n}", f"target\t\t{term(subj(c['parent']))} a :{c['name']} .", f"source\t\tSELECT * FROM {dm[c['parent']]['sql_name']} WHERE \"{colsql}\" = '{val.replace(chr(39), chr(39)*2)}'", ""]
    for p in model["objprops"]:
        n += 1
        if p["join"]:
            # Join table: build both referenced IRI templates from the FK columns; one join-table row = one triple
            def tmpl(fk):
                ru = fk["ref_table"].upper()
                ph = {rc.upper(): "{" + (dm[p["table"]]["cols"].get(fc.upper()) or {"name": fc})["name"] + "}" for rc, fc in zip(fk["ref_columns"], fk["columns"])}
                return subj(ru, ph)
            L += [f"mappingId\tM{n}", f"target\t\t{term(tmpl(p['join']['dom_fk']))} :{p['name']} {term(tmpl(p['join']['rng_fk']))} .", f"source\t\tSELECT * FROM {dm[p['table']]['sql_name']}", ""]
        elif p.get("manual"):
            # steward-added relation with no foreign key in the database: object IRI built from this table's columns
            ph = {rc.upper(): "{" + (dm[p["table"]]["cols"].get(c.upper()) or {"name": c})["name"] + "}" for c, rc in zip(p["cols"], p["manual"]["ref_cols"])}
            L += [f"mappingId\tM{n}", f"target\t\t{term(subj(p['table']))} :{p['name']} {term(subj(p['range_t'], ph))} .", f"source\t\tSELECT * FROM {dm[p['table']]['sql_name']}", ""]
        else:
            fk = next(f for f in dm[p["table"]]["fks"] if f["cols"] == [c.upper() for c in p["cols"]])
            m = fk["map"]
            obj = next(o for pr, o in m["triples"] if pr == fk["pred"]).strip("<>")
            subj_t = subj(p["table"], ph_of(m["subject"], p["table"]))
            obj_t = subj(fk["ref"], ph_of(obj, fk["ref"]))
            L += [f"mappingId\tM{n}", f"target\t\t{term(subj_t)} :{p['name']} {term(obj_t)} .", f"source\t\t{m['source']}", ""]
    L.append("]]")
    path.write_text("\n".join(L) + "\n")


# ──────────────────────────────── E. Scoring ────────────────────────────────


def name_tokens(s: str) -> set[str]:
    return set(el.words(s).lower().split())


def score(model: dict, tables: list[dict], gold: dict) -> None:
    kept_cls = {tu for tu in model["classes"] if not model["classes"][tu].get("stub")}
    kept_col = {(p["table"], p["col"]): p["name"] for p in model["dtprops"]}
    kept_fk = {(p["table"], tuple(p["cols"])): p["name"] for p in model["objprops"] if not p["join"]}
    for p in model["objprops"]:
        if p["join"]:
            for fk in (p["join"]["dom_fk"], p["join"]["rng_fk"]):
                kept_fk[(p["table"], tuple(x.upper() for x in fk["columns"]))] = p["name"]
    by_name = {t["name"].upper(): t for t in tables}

    def report(name, rows):
        # rows: (key, gold_answers, produced_name|None, technical: bool)  technical = none is acceptable under the lenient rule (identifier/FK columns, subtype and self links)
        st = {"strict": [0, 0, 0], "lenient": [0, 0, 0]}  # tp fp fn
        notes, sims, exact = [], [], 0
        for key, answers, produced, technical in rows:
            want = any(a != "none" for a in answers)
            optional = "none" in answers and want
            if produced and want:
                for k in st:
                    st[k][0] += 1
                g = max((len(name_tokens(produced) & name_tokens(a)) / max(1, len(name_tokens(produced) | name_tokens(a))) for a in answers if a != "none"))
                sims.append(g)
                if any(name_tokens(produced) == name_tokens(a) for a in answers if a != "none"):
                    exact += 1
                elif g < 0.5:
                    notes.append(f"~ {key}: {produced} (gold {answers})")
            elif produced and not want:
                st["strict"][1] += 1
                if technical:
                    st["lenient"][1] += 1
                    notes.append(f"x {key}: kept {produced} (technical column/link, should be pruned)")
            elif not produced and want and not optional:
                for k in st:
                    st[k][2] += 1
                notes.append(f"x {key}: pruned (gold {answers})")
        for k, (tp, fp, fn) in st.items():
            p, r, f = el.prf(tp, fp, fn)
            print(f"{name:13s} {k:8s} P={p:.3f} R={r:.3f} F1={f:.3f}  (tp={tp} fp={fp} fn={fn})")
        if sims:
            print(f"{'':13s} name match: exact {exact}/{len(sims)}, mean token-jaccard {sum(sims)/len(sims):.2f}")
        for n in notes:
            print("    " + n)

    rows = []
    for t, a in gold["tables"].items():
        subs = [c["name"] for k, c in model["classes"].items() if c.get("parent") == t.upper() and c.get("filter")]
        produced = "+".join(subs) if subs else (model["classes"].get(t.upper(), {}).get("name") if t.upper() in kept_cls else None)
        rows.append((t, a, produced, False))
    report("tables", rows)
    rows = []
    for key, a in gold["columns"].items():
        t, c = key.split(".")
        ci = next(x for x in by_name[t.upper()]["columns"] if x["name"].upper() == c.upper())
        rows.append((key, a, kept_col.get((t.upper(), c.upper())), ci["fk"] or (ci["pk"] and len(by_name[t.upper()]["pk"]) == 1)))
    report("columns", rows)
    rows = []
    for key, a in gold["foreign_keys"].items():
        src, ref = key.split(" -> ")
        t, cols = src.split(".", 1)
        tt = by_name[t.upper()]
        colset = sorted(x.upper() for x in cols.split(","))
        pkset = sorted(x.upper() for x in tt["pk"])
        technical = (ref.upper() == t.upper() and colset == pkset) or ((tt["parent"] or "").upper() == ref.upper() and colset == pkset)
        rows.append((key, a, kept_fk.get((t.upper(), tuple(c.upper() for c in cols.split(",")))), technical))
    report("foreign_keys", rows)


# ──────────────────────────────── main ────────────────────────────────


def run_pipeline(tables: list[dict], stats: dict, boot_obda: Path, level: str = "none", domain: str | None = None, schema: str | None = None,
                 ns: str = "http://example.org/ontology/v1/", do_harmonize: bool = True, log=print) -> dict:
    """Catalog + Direct Mapping -> proposed model. Also called from the server. Call el.set_provider() beforehand."""
    dm = direct_mapping(parse_obda(boot_obda, schema))
    props, usage = {}, [0, 0]
    names = [t["name"] for t in tables]
    for t in tables:
        ts = stats.get(t["name"].upper())
        out = propose(t, names, domain, tstats=ts, level=level)
        if out["table"].get("kind") == "join" and not t["join_table"]:
            nonkey = [c["name"] for c in t["columns"] if not c["pk"] and not c["fk"]]
            out = propose(t, names, domain, f"NOT a pure join table: it has non-key attributes ({', '.join(nonkey)}); model it as an entity (association class) with object properties for its foreign keys", tstats=ts, level=level)
        props[t["name"]] = out
        if "_usage" in out:
            usage[0] += out["_usage"]["in"]
            usage[1] += out["_usage"]["out"]
        log(f"  {t['name']}: kind={out['table'].get('kind')} class={out['table'].get('class_name')} keep={sum(1 for c in out.get('columns', []) if c.get('keep'))}/{len(t['columns'])}"
            + (" subclasses=" + ",".join(f"{x.get('column')}={x.get('value')}:{x.get('class_name')}" for x in out.get("subclasses_by_value") or []) if out.get("subclasses_by_value") else ""))
    model = build_model(tables, props, dm, ns)
    renames = {}
    if do_harmonize:
        vocab = [("class", c["name"], tu.lower()) for tu, c in model["classes"].items() if not c.get("stub")]
        vocab += [("datatype-property", p["name"], f"{p['table'].lower()}.{p['col'].lower()}") for p in model["dtprops"]]
        vocab += [("object-property", p["name"], f"{p['table'].lower()} -> {p['range_t'].lower()}") for p in model["objprops"]]
        renames = harmonize(vocab)
        apply_renames(model, renames)
        if renames:
            log(f"harmonize: {len(renames)} renames: " + ", ".join(f"{a}->{b}" for a, b in renames.items()))
    if do_harmonize:
        propose_superclasses(model)
        if model["superclasses"]:
            log("superclasses: " + ", ".join(f"{sc['name']} <- {'/'.join(model['classes'][m]['name'] for m in sc['members'])}" for sc in model["superclasses"]))
    log(f"LLM tokens (uncached calls only): in={usage[0]} out={usage[1]}")
    return {"model": model, "props": props, "renames": renames, "dm": dm}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=str(ROOT / "bench" / "insurance" / "catalog.json"), help="catalog JSON from scan/extract_pg.py; 'yaml' uses the el_proto benchmark YAML instead")
    ap.add_argument("--boot", default=str(BOOT_OBDA), help="Ontop bootstrap .obda")
    ap.add_argument("--gold", default=None, help="gold YAML for scoring. Default: gold_r2rml.yaml for the YAML input and the demo database catalog, otherwise no scoring")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--provider", choices=list(el.PROVIDERS), default="deepseek")
    ap.add_argument("--domain", default=None, help="business domain hint (e.g. 'P&C insurance'). If omitted the LLM infers it from the schema")
    ap.add_argument("--no-harmonize", action="store_true")
    ap.add_argument("--ns", default="http://example.org/ontology/v1/")
    ap.add_argument("--data-ns", default="http://example.org/data/")
    ap.add_argument("--stats", default=str(ROOT / "bench" / "insurance" / "stats.json"), help="optional column profile JSON for the YAML input")
    ap.add_argument("--stats-level", choices=["none", "counts", "values"], default="none", help="how much of the column profile the LLM sees (external LLM = counts / in-house LLM = values)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    el.set_provider(args.provider)
    out_dir = Path(args.out)
    out_dir.mkdir(exist_ok=True, parents=True)
    boot = Path(args.boot)
    if args.catalog != "yaml":
        tables, stats = load_catalog(Path(args.catalog))
        gold_path = Path(args.gold) if args.gold else (GOLD if "bench/insurance/catalog.json" in args.catalog else None)  # score against the gold only for the demo database
    else:
        tables = el.load_schema()
        derive_structure(tables)
        stats = json.loads(Path(args.stats).read_text()) if Path(args.stats).exists() else {}
        gold_path = Path(args.gold) if args.gold else GOLD
    print(f"input: {len(tables)} tables from {args.catalog or 'el_proto YAML'}; direct mapping from {boot}; llm={el.LLM_MODEL}; domain={args.domain or '(infer)'}; stats-level={args.stats_level}")
    res = run_pipeline(tables, stats, boot, level=args.stats_level, domain=args.domain, ns=args.ns, do_harmonize=not args.no_harmonize, log=print if args.verbose else (lambda *_: None))
    model, props, renames, dm = res["model"], res["props"], res["renames"], res["dm"]
    if renames and not args.verbose:
        print(f"harmonize: {len(renames)} renames: " + ", ".join(f"{a}->{b}" for a, b in renames.items()))
    write_ttl(model, out_dir / "v1.ttl")
    write_obda(model, dm, out_dir / "v1.obda", args.data_ns)
    (out_dir / "review.json").write_text(json.dumps({"proposals": props, "renames": renames, "pruned": model["pruned"]}, ensure_ascii=False, indent=1))
    print(f"output: {len(model['classes'])} classes, {len(model['dtprops'])} datatype props, {len(model['objprops'])} object props, pruned {len(model['pruned'])}  → {out_dir}/v1.ttl, v1.obda, review.json")
    if gold_path and gold_path.exists():
        print()
        score(model, tables, yaml.safe_load(gold_path.read_text()))
    return


if __name__ == "__main__":
    main()
