#!/usr/bin/env python3
"""Schema -> ontology entity-linking prototype.

Stages:
  1. extract    read pc-insurance-tables.yaml (13 tables, 65 columns, PK/FK). No LLM
  2. normalize  split table and column names into words and expand abbreviations. No LLM
  3a. candidates top-k targets by embedding similarity + string match
  3b. judge     per table, give the LLM only the candidates and let it choose (none allowed)
  7. evaluate   compare with the gold set: recall@k, P/R/F1

Targets = insurance.ttl (the correct ontology) + schemaorg.ttl (distractor, optional: download
https://schema.org/version/latest/schemaorg-current-https.ttl to el_proto/data/schemaorg.ttl to enable it).
Runs serially. Embeddings and LLM responses are cached by input hash.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import yaml
from openai import OpenAI
from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef

HERE = Path(__file__).parent
DATA = HERE / "data"
CACHE = Path(os.environ.get("COA_CACHE_DIR", str(HERE / "cache")))  # LLM / embedding response cache
SCHEMA_ORG = Namespace("https://schema.org/")
EMBED_MODEL = "text-embedding-3-small"

# LLM providers: openai (gpt-4.1-mini) / deepseek (deepseek-chat, OpenAI-compatible API) / ollama (local)
PROVIDERS = {
    "openai": {"model": "gpt-4.1-mini", "base_url": None, "env": "OPENAI_API_KEY"},
    "deepseek": {"model": "deepseek-chat", "base_url": "https://api.deepseek.com", "env": "DEEPSEEK_API_KEY"},
    "ollama": {"model": os.environ.get("OLLAMA_MODEL", "mistral:latest"), "base_url": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"), "env": None},
}
LLM_MODEL = "gpt-4.1-mini"
client = None  # embeddings client (needs an OpenAI key); created on first use so the app starts without one
llm_client = None


def set_provider(name: str) -> None:
    global LLM_MODEL, llm_client
    import os

    cfg = PROVIDERS[name]
    LLM_MODEL = cfg["model"]
    llm_client = OpenAI(api_key=os.environ[cfg["env"]] if cfg["env"] else "ollama", base_url=cfg["base_url"])

# ──────────────────────────────── 1. Extract ────────────────────────────────


def load_schema() -> list[dict]:
    doc = yaml.safe_load((DATA / "pc-insurance-tables.yaml").read_text())
    tables = []
    for t in doc["tables"]:
        pk = set(t.get("primaryKey", []))
        fks = t.get("foreignKeys", [])
        fk_cols = {c for fk in fks for c in fk["columns"]}
        cols = []
        for c in t["columns"]:
            cols.append(
                {
                    "name": c["name"],
                    "type": c["dataType"].lower() + (f"({c['dataLength']})" if c.get("dataLength") else ""),
                    "pk": c["name"] in pk,
                    "fk": c["name"] in fk_cols,
                    "nullable": c.get("constraint") != "NOT_NULL",
                }
            )
        tables.append(
            {
                "name": t["name"],
                "columns": cols,
                "pk": sorted(pk),
                "fks": [
                    {"columns": fk["columns"], "ref_table": fk["referredTable"], "ref_columns": fk["referredColumns"]}
                    for fk in fks
                ],
            }
        )
    # Structure that can be derived deterministically: parent/child (PK is also an FK = subtype), join table (PK made only of FK columns)
    by_name = {t["name"]: t for t in tables}
    for t in tables:
        t["parent"] = next((fk["ref_table"] for fk in t["fks"] if sorted(fk["columns"]) == t["pk"] and fk["ref_table"] != t["name"]), None)
        t["children"] = []
        fk_cols = {c for fk in t["fks"] for c in fk["columns"]}
        t["join_table"] = len(t["fks"]) >= 2 and set(t["pk"]) <= fk_cols and all(c["pk"] or c["fk"] for c in t["columns"])
    for t in tables:
        if t["parent"] in by_name:
            by_name[t["parent"]]["children"].append(t["name"])
    return tables


# ──────────────────────────────── 2. Normalize ───────────────────────────────

ABBREV = {
    "id": "identifier",
    "cd": "code",
    "amt": "amount",
    "dt": "date",
    "num": "number",
    "desc": "description",
}


def words(ident: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", ident)  # camelCase
    s = s.replace("_", " ").lower()
    return " ".join(ABBREV.get(w, w) for w in s.split())


# ──────────────────────────────── Targets ─────────────────────────────────


def local(uri) -> str:
    s = str(uri)
    return s.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def load_targets() -> list[dict]:
    """Collect classes / object properties / datatype properties from insurance.ttl and schemaorg.ttl."""
    targets: list[dict] = []

    def lit(g, s, p):
        v = g.value(s, p)
        return str(v) if v is not None else ""

    # The correct ontology (+ a helper file with vocabulary that appears only in the R2RML)
    g = Graph().parse(DATA / "insurance.ttl", format="turtle")
    if (DATA / "insurance_r2rml_extra.ttl").exists():
        g.parse(DATA / "insurance_r2rml_extra.ttl", format="turtle")
    for s in g.subjects(RDF.type, OWL.Class):
        targets.append({"uri": str(s), "kind": "class", "label": lit(g, s, RDFS.label), "comment": "", "src": "insurance"})
    for s in g.subjects(RDF.type, OWL.ObjectProperty):
        targets.append(
            {"uri": str(s), "kind": "objprop", "label": lit(g, s, RDFS.label), "comment": "",
             "domain": local(g.value(s, RDFS.domain) or ""), "range": local(g.value(s, RDFS.range) or ""), "src": "insurance"}
        )
    for s in g.subjects(RDF.type, OWL.DatatypeProperty):
        targets.append(
            {"uri": str(s), "kind": "dtprop", "label": lit(g, s, RDFS.label), "comment": "",
             "domain": local(g.value(s, RDFS.domain) or ""), "range": local(g.value(s, RDFS.range) or ""), "src": "insurance"}
        )

    # Schema.org (distractor). rdfs:Class / rdf:Property. A property whose range is Text/Number/Date-like is treated as a datatype property
    if not (DATA / "schemaorg.ttl").exists():
        return targets
    g = Graph().parse(DATA / "schemaorg.ttl", format="turtle")
    datatypes = {"Text", "Number", "Integer", "Float", "Date", "DateTime", "Time", "Boolean", "URL"}
    for s in g.subjects(RDF.type, RDFS.Class):
        if not str(s).startswith(str(SCHEMA_ORG)):
            continue
        targets.append({"uri": str(s), "kind": "class", "label": lit(g, s, RDFS.label), "comment": lit(g, s, RDFS.comment)[:200], "src": "schema.org"})
    for s in g.subjects(RDF.type, RDF.Property):
        if not str(s).startswith(str(SCHEMA_ORG)):
            continue
        ranges = [local(o) for o in g.objects(s, SCHEMA_ORG.rangeIncludes)]
        domains = [local(o) for o in g.objects(s, SCHEMA_ORG.domainIncludes)]
        kind = "dtprop" if ranges and all(r in datatypes for r in ranges) else "objprop"
        targets.append(
            {"uri": str(s), "kind": kind, "label": lit(g, s, RDFS.label), "comment": lit(g, s, RDFS.comment)[:200],
             "domain": ",".join(domains[:3]), "range": ",".join(ranges[:3]), "src": "schema.org"}
        )
    return targets


def target_text(t: dict) -> str:
    parts = [words(t["label"] or local(t["uri"]))]
    if t.get("domain"):
        parts.append(f"domain: {t['domain']}")
    if t.get("range"):
        parts.append(f"range: {t['range']}")
    if t.get("comment"):
        parts.append(t["comment"])
    return " | ".join(parts)


# ──────────────────────────────── 3a. Candidates ────────────────────────────────


def _cache_path(kind: str, key: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    return CACHE / f"{kind}_{hashlib.sha256(key.encode()).hexdigest()[:24]}.json"


def embed(texts: list[str]) -> np.ndarray:
    """OpenAI embeddings, cached per text."""
    global client
    if client is None:
        client = OpenAI()
    out: list[np.ndarray | None] = [None] * len(texts)
    todo = []
    for i, tx in enumerate(texts):
        p = _cache_path("emb", EMBED_MODEL + "\n" + tx)
        if p.exists():
            out[i] = np.array(json.loads(p.read_text()))
        else:
            todo.append(i)
    for start in range(0, len(todo), 500):
        batch = todo[start : start + 500]
        resp = client.embeddings.create(model=EMBED_MODEL, input=[texts[i] for i in batch])
        for i, d in zip(batch, resp.data):
            out[i] = np.array(d.embedding)
            _cache_path("emb", EMBED_MODEL + "\n" + texts[i]).write_text(json.dumps(d.embedding))
    m = np.stack(out)  # type: ignore[arg-type]
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def top_k(query_vec: np.ndarray, pool_idx: list[int], target_vecs: np.ndarray, k: int) -> list[int]:
    sims = target_vecs[pool_idx] @ query_vec
    order = np.argsort(-sims)[:k]
    return [pool_idx[i] for i in order]


class BM25:
    """Dependency-free BM25 over the normalized word sequences. The default retriever when there is no embedding key."""

    def __init__(self, docs: list[str], k1: float = 1.2, b: float = 0.75):
        import math
        from collections import Counter

        self.k1, self.b = k1, b
        self.toks = [d.split() for d in docs]
        self.n = len(docs)
        self.avgdl = sum(len(t) for t in self.toks) / max(self.n, 1)
        df: Counter = Counter()
        for t in self.toks:
            df.update(set(t))
        self.idf = {w: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.tf = [Counter(t) for t in self.toks]

    def score(self, query: str, i: int) -> float:
        tf, dl = self.tf[i], len(self.toks[i])
        s = 0.0
        for w in query.split():
            if w not in tf:
                continue
            f = tf[w]
            s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return s

    def top_k(self, query: str, pool_idx: list[int], k: int) -> list[int]:
        scored = sorted(((self.score(query, i), i) for i in pool_idx), key=lambda x: -x[0])
        return [i for _, i in scored[:k]]


def string_matches(name: str, pool: list[int], targets: list[dict]) -> list[int]:
    """Force exact matches of the normalized label into the candidates."""
    w = words(name)
    return [i for i in pool if words(targets[i]["label"] or local(targets[i]["uri"])) == w]


def build_candidates(tables: list[dict], k: int, retriever: str = "bm25") -> dict[str, dict]:
    """Build target candidates (indexes into TARGETS) per table, column and FK. {table: {table: [...], columns: {col: [...]}, fks: {cols: [...]}}}"""
    pool = {kind: [i for i, t in enumerate(TARGETS) if t["kind"] == kind] for kind in ("class", "dtprop", "objprop")}
    queries, qkeys = [], []
    for t in tables:
        queries.append(f"table {words(t['name'])} with columns: " + ", ".join(words(c["name"]) for c in t["columns"]))
        qkeys.append(("table", t["name"]))
        for c in t["columns"]:
            queries.append(f"column {words(c['name'])} ({c['type']}) of table {words(t['name'])}")
            qkeys.append(("column", f"{t['name']}.{c['name']}"))
        for fk in t["fks"]:
            queries.append(f"relation from {words(t['name'])} to {words(fk['ref_table'])} via {words(' '.join(fk['columns']))}")
            qkeys.append(("fk", f"{t['name']}.{','.join(fk['columns'])} -> {fk['ref_table']}"))
    if retriever == "embed":
        tvecs = embed([target_text(t) for t in TARGETS])
        qvecs = embed(queries)
        retrieve = lambda i, kind: top_k(qvecs[i], pool[kind], tvecs, k)  # noqa: E731
    else:
        bm25 = BM25([words(target_text(t)) for t in TARGETS])
        retrieve = lambda i, kind: bm25.top_k(words(queries[i]), pool[kind], k)  # noqa: E731

    # Small vocabularies (the customer ontology) go into the candidates in full. Narrowing only applies to large vocabularies (Schema.org etc.)
    small = {kind: [i for i in pool[kind] if TARGETS[i]["src"] in SMALL_SOURCES] for kind in pool}

    cands: dict[str, dict] = {t["name"]: {"table": [], "columns": {}, "fks": {}} for t in tables}
    for qi, (kind, key) in enumerate(qkeys):
        if kind == "table":
            forced = string_matches(key, pool["class"], TARGETS)
            cands[key]["table"] = list(dict.fromkeys(forced + small["class"] + retrieve(qi, "class")))
        elif kind == "column":
            tn, cn = key.split(".")
            forced = string_matches(cn, pool["dtprop"], TARGETS)
            cands[tn]["columns"][cn] = list(dict.fromkeys(forced + small["dtprop"] + retrieve(qi, "dtprop")))
        else:
            tn, rest = key.split(".", 1)
            cands[tn]["fks"][rest.split(" -> ")[0]] = list(dict.fromkeys(small["objprop"] + retrieve(qi, "objprop")))
    return cands


SMALL_SOURCES = {"insurance"}  # vocabularies always included in the candidates (the small ones)


# ──────────────────────────────── 3b. LLM judgment ───────────────────────────

SYSTEM = """You map relational database tables to an existing OWL ontology.
For each table you receive its columns and foreign keys plus candidate ontology terms.
Choose, for the table, at most one class; for each column, at most one datatype property;
for each foreign key, at most one object property (direction: the table's row is the domain).
Choose "none" when no candidate genuinely represents the item. Surrogate identifier columns map to "none".

Structural rules (the structure facts are given to you; apply them):
- SUBTYPE: a table whose primary key is also a foreign key to a parent table is a subtype of the parent.
  The parent's non-key columns belong to each subtype class too. When a parent column (e.g. an amount)
  is realised in the ontology as one datatype property per subtype, return ALL of them as a list in "uri".
- ROLE: a table whose class depends on a code column (e.g. a party-role code) represents several classes.
  Return them as a list in "uri" (only classes clearly supported by candidates).
- JOIN: a pure join table (primary key made only of foreign keys) usually maps to "none" as a class, but the
  pair of foreign keys realises ONE object property between the two referenced entities. Put that property
  on the foreign key that points to the property's domain and set "inverse": false; put "none" on the other.
- DIRECTION: by default the table's row is the property's domain. If the only fitting candidate has the
  referenced table as its domain and this table as its range, you may still choose it with "inverse": true.
  A subtype table's link to its parent is "none".

Output raw JSON only, matching:
{"table": {"uri": "<uri | [uri, ...] | none>", "confidence": 0.0-1.0, "reason": "..."},
 "columns": [{"name": "...", "uri": "<uri | [uri, ...] | none>", "confidence": 0.0-1.0, "reason": "..."}],
 "foreign_keys": [{"columns": ["..."], "uri": "<uri | [uri, ...] | none>", "inverse": false, "confidence": 0.0-1.0, "reason": "..."}]}"""


def fmt_cands(idxs: list[int], targets: list[dict]) -> str:
    lines = []
    for i in idxs:
        t = targets[i]
        extra = ""
        if t.get("domain") or t.get("range"):
            extra = f" [domain={t.get('domain') or '?'} range={t.get('range') or '?'}]"
        lines.append(f"  - {t['uri']}  \"{t['label']}\"{extra} {t['comment'][:120]}")
    return "\n".join(lines)


def judge(table: dict, cands: dict) -> dict:
    cols = "\n".join(
        f"  - {c['name']} {c['type']}{' PK' if c['pk'] else ''}{' FK' if c['fk'] else ''}" for c in table["columns"]
    )
    fks = "\n".join(f"  - ({', '.join(fk['columns'])}) -> {fk['ref_table']}" for fk in table["fks"]) or "  (none)"
    facts = []
    if table["parent"]:
        facts.append(f"SUBTYPE of {table['parent']} (primary key is a foreign key to it)")
    if table["children"]:
        facts.append(f"SUPERTYPE with subtype tables: {', '.join(table['children'])}")
    if table["join_table"]:
        facts.append("JOIN table (primary key consists only of foreign keys)")
    user = [
        f"Table: {table['name']}  (words: {words(table['name'])})",
        "Structure facts: " + ("; ".join(facts) if facts else "ordinary entity table"),
        "Columns:", cols, "Foreign keys:", fks, "",
    ]
    user.append("Candidate classes for the table:")
    user.append(fmt_cands(cands["table"], TARGETS))
    for c in table["columns"]:
        user.append(f"\nCandidate datatype properties for column {c['name']}:")
        user.append(fmt_cands(cands["columns"][c["name"]], TARGETS))
    for fk in table["fks"]:
        key = ",".join(fk["columns"])
        user.append(f"\nCandidate object properties for foreign key ({key}) -> {fk['ref_table']}:")
        user.append(fmt_cands(cands["fks"][key], TARGETS))
    prompt = "\n".join(user)

    p = _cache_path("llm", LLM_MODEL + "\n" + SYSTEM + "\n" + prompt)
    if p.exists():
        return json.loads(p.read_text())
    resp = llm_client.chat.completions.create(
        model=LLM_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
    )
    out = json.loads(resp.choices[0].message.content or "{}")
    out["_usage"] = {"in": resp.usage.prompt_tokens, "out": resp.usage.completion_tokens}
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    return out


# ──────────────────────────────── 7. Evaluate ────────────────────────────────


def norm_answer(uri, prefix: str) -> str:
    """uri | [uri,...] | none -> 'none' or names joined as 'A+B' (namespaces outside the gold stay as full URIs, so they are always wrong)."""
    if not uri or uri == "none":
        return "none"
    uris = uri if isinstance(uri, list) else [uri]
    names = [local(u) if str(u).startswith(prefix) else str(u) for u in uris if u and u != "none"]
    return "+".join(sorted(names)) if names else "none"


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--no-llm", action="store_true", help="candidate generation and recall@k only")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--retriever", choices=["bm25", "embed"], default="bm25")
    ap.add_argument("--provider", choices=list(PROVIDERS), default="openai")
    ap.add_argument("--gold", default="gold_r2rml.yaml", help="gold file (gold.yaml = derived by hand, gold_r2rml.yaml = derived from R2RML)")
    args = ap.parse_args()
    set_provider(args.provider)

    global TARGETS
    tables = load_schema()
    TARGETS = load_targets()
    gold = yaml.safe_load((HERE / args.gold).read_text())
    prefix = gold["prefix"]
    n_cls = sum(t["kind"] == "class" for t in TARGETS)
    n_dt = sum(t["kind"] == "dtprop" for t in TARGETS)
    n_obj = sum(t["kind"] == "objprop" for t in TARGETS)
    print(f"schema: {len(tables)} tables, {sum(len(t['columns']) for t in tables)} columns")
    print(f"targets: {n_cls} classes, {n_dt} datatype props, {n_obj} object props  (k={args.k}, retriever={args.retriever}, llm={LLM_MODEL})")

    cands = build_candidates(tables, args.k, args.retriever)

    # recall@k (only items whose gold is not none)
    def recall(section: str, getter) -> None:
        hit = tot = 0
        misses = []
        for key, answers in gold[section].items():
            if answers == ["none"]:
                continue
            tot += 1
            names = {local(TARGETS[i]["uri"]) for i in getter(key) if TARGETS[i]["src"] == "insurance"}
            if names & set(answers):
                hit += 1
            else:
                misses.append(key)
        print(f"recall@{args.k} {section:13s}: {hit}/{tot} = {hit / tot if tot else 0:.3f}  misses={misses}")

    recall("tables", lambda key: cands[key]["table"])
    recall("columns", lambda key: cands[key.split('.')[0]]["columns"][key.split('.')[1]])
    recall("foreign_keys", lambda key: cands[key.split('.')[0]]["fks"][key.split('.', 1)[1].split(' -> ')[0]])
    if args.no_llm:
        return

    # LLM judgment
    usage_in = usage_out = 0
    results = {}
    for t in tables:
        r = judge(t, cands[t["name"]])
        usage_in += r.get("_usage", {}).get("in", 0)
        usage_out += r.get("_usage", {}).get("out", 0)
        results[t["name"]] = r
        if args.verbose:
            print(f"\n## {t['name']} -> {norm_answer(r['table'].get('uri'), prefix)} ({r['table'].get('confidence')}) {r['table'].get('reason', '')[:100]}")
            for c in r.get("columns", []):
                print(f"   {c['name']:40s} -> {norm_answer(c.get('uri'), prefix)} ({c.get('confidence')})")
            for fk in r.get("foreign_keys", []):
                inv = " [inverse]" if fk.get("inverse") else ""
                print(f"   FK({','.join(fk.get('columns', []))}) -> {norm_answer(fk.get('uri'), prefix)}{inv} ({fk.get('confidence')})")
    print(f"\nLLM tokens (uncached calls only): in={usage_in} out={usage_out}")

    # P/R/F1. Only items that have a gold row are scored. tp = matches gold, fp = chose something wrong, fn = chose none although gold has an answer
    def score(section: str, pairs: list[tuple[str, str]]) -> None:
        tp = fp = fn = tn = 0
        wrong = []
        for key, pred in pairs:
            answers = gold[section].get(key)
            if answers is None:
                continue
            preds = pred.split("+")
            if pred == "none" and answers == ["none"]:
                tn += 1
            elif pred != "none" and set(preds) <= set(answers):  # a multi-answer is correct when all of it is inside the gold
                tp += 1
            elif pred == "none":
                fn += 1
                wrong.append(f"{key}: none (gold {answers})")
            else:
                fp += 1
                if answers != ["none"]:
                    fn += 1
                wrong.append(f"{key}: {pred} (gold {answers})")
        p, r, f = prf(tp, fp, fn)
        print(f"{section:13s} P={p:.3f} R={r:.3f} F1={f:.3f}  (tp={tp} fp={fp} fn={fn} correct-none={tn})")
        for w in wrong:
            print(f"    x {w}")

    tpairs, cpairs, fpairs = [], [], []
    for tn_, r in results.items():
        tpairs.append((tn_, norm_answer(r.get("table", {}).get("uri"), prefix)))
        for c in r.get("columns", []):
            cpairs.append((f"{tn_}.{c.get('name')}", norm_answer(c.get("uri"), prefix)))
        for fk in r.get("foreign_keys", []):
            cols = ",".join(fk.get("columns", []))
            ref = next((x["ref_table"] for x in next(t for t in tables if t["name"] == tn_)["fks"] if ",".join(x["columns"]) == cols), "?")
            fpairs.append((f"{tn_}.{cols} -> {ref}", norm_answer(fk.get("uri"), prefix)))
    print()
    score("tables", tpairs)
    score("columns", cpairs)
    score("foreign_keys", fpairs)


TARGETS: list[dict] = []

if __name__ == "__main__":
    main()
