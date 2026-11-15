#!/usr/bin/env python3
"""Scan: Postgres -> catalog JSON (the Scan -> Model boundary). No LLM, deterministic extraction only.

Extracts: tables, columns (type / nullable), primary keys, foreign keys (composite, order preserved), column profiles (rows / non-null / distinct / top values / min / max).
Calls psql via subprocess (no Python database driver needed). Swapping in psycopg later keeps the same catalog format.
Usage: PGPASSWORD=... python3 extract_pg.py "host=HOST port=PORT dbname=DB user=USER" [--schema public] [--top 10] [--no-values] -o catalog.json
(libpq reads the password from PGPASSWORD or ~/.pgpass; keep it off the command line)
  --no-values: skip top values / min / max (for deployments that must not show data values to an external LLM; counts are kept)
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys


def psql(conninfo: str, sql: str) -> list[dict]:
    r = subprocess.run(["psql", conninfo, "--csv", "-v", "ON_ERROR_STOP=1", "-c", sql], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(r.stderr)
    return list(csv.DictReader(io.StringIO(r.stdout)))


def pg_array(s: str) -> list[str]:
    s = s.strip()
    return [x.strip('"') for x in s[1:-1].split(",")] if s.startswith("{") and len(s) > 2 else []


def q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("conninfo")
    ap.add_argument("--schema", default="public")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--no-values", action="store_true")
    ap.add_argument("-o", "--out", default="catalog.json")
    a = ap.parse_args()
    sch = a.schema.replace("'", "''")

    cols = psql(a.conninfo, f"""
        SELECT table_name, column_name, ordinal_position, data_type, character_maximum_length, numeric_precision, numeric_scale, is_nullable
        FROM information_schema.columns WHERE table_schema='{sch}' ORDER BY table_name, ordinal_position""")
    keys = psql(a.conninfo, f"""
        SELECT con.contype, cl.relname AS table_name, coalesce(rcl.relname, '') AS ref_table,
               array_agg(a.attname ORDER BY k.ord) AS cols,
               array_agg(coalesce(ra.attname, '') ORDER BY k.ord) AS ref_cols
        FROM pg_constraint con
        JOIN pg_class cl ON cl.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = cl.relnamespace
        LEFT JOIN pg_class rcl ON rcl.oid = con.confrelid
        CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(attnum, rattnum, ord)
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
        LEFT JOIN pg_attribute ra ON ra.attrelid = con.confrelid AND ra.attnum = k.rattnum
        WHERE con.contype IN ('p','f') AND ns.nspname = '{sch}'
        GROUP BY con.oid, con.contype, cl.relname, rcl.relname ORDER BY 2, 1""")

    tables: dict[str, dict] = {}
    for c in cols:
        t = tables.setdefault(c["table_name"], {"name": c["table_name"], "columns": [], "pk": [], "fks": [], "rows": 0})
        ty = c["data_type"]
        if c["character_maximum_length"]:
            ty += f"({c['character_maximum_length']})"
        elif ty == "numeric" and c["numeric_precision"]:
            ty += f"({c['numeric_precision']},{c['numeric_scale'] or 0})"
        t["columns"].append({"name": c["column_name"], "type": ty, "nullable": c["is_nullable"] == "YES", "pk": False, "fk": False})
    for k in keys:
        t = tables.get(k["table_name"])
        if not t:
            continue
        cs = pg_array(k["cols"])
        if k["contype"] == "p":
            t["pk"] = cs
        else:
            t["fks"].append({"columns": cs, "ref_table": k["ref_table"], "ref_columns": pg_array(k["ref_cols"])})
    for t in tables.values():
        fkcols = {c for fk in t["fks"] for c in fk["columns"]}
        for c in t["columns"]:
            c["pk"] = c["name"] in t["pk"]
            c["fk"] = c["name"] in fkcols

    # Profile: one query for the counts, one for the top values (large production tables should use TABLESAMPLE per column)
    parts = []
    for t in tables.values():
        for c in t["columns"]:
            col = f"{q(t['name'])}.{q(c['name'])}"
            mm = f"min({col})::text AS min, max({col})::text AS max" if not a.no_values else "NULL::text AS min, NULL::text AS max"
            parts.append(f"SELECT '{t['name']}' t, '{c['name']}' c, count(*) n, count({col}) nn, count(distinct {col}) nd, {mm} FROM {q(sch)}.{q(t['name'])}")
    for r in psql(a.conninfo, " UNION ALL ".join(parts)):
        t = tables[r["t"]]
        t["rows"] = int(r["n"])
        c = next(x for x in t["columns"] if x["name"] == r["c"])
        c["stats"] = {"non_null": int(r["nn"]), "distinct": int(r["nd"]), "min": r["min"] or None, "max": r["max"] or None, "top": []}
    if not a.no_values:
        parts = []
        for t in tables.values():
            for c in t["columns"]:
                col = f"{q(t['name'])}.{q(c['name'])}"
                parts.append(f"(SELECT '{t['name']}' t, '{c['name']}' c, {col}::text v, count(*) n FROM {q(sch)}.{q(t['name'])} WHERE {col} IS NOT NULL GROUP BY 3 ORDER BY 4 DESC, 3 LIMIT {a.top})")
        for r in psql(a.conninfo, " UNION ALL ".join(parts)):
            c = next(x for x in tables[r["t"]]["columns"] if x["name"] == r["c"])
            c["stats"]["top"].append([r["v"], int(r["n"])])

    cat = {"source": {"kind": "postgres", "schema": a.schema, "values_included": not a.no_values}, "tables": list(tables.values())}
    with open(a.out, "w") as f:
        json.dump(cat, f, ensure_ascii=False, indent=1)
    print(f"catalog: {len(tables)} tables, {sum(len(t['columns']) for t in tables.values())} columns, {sum(len(t['fks']) for t in tables.values())} fks → {a.out}")


if __name__ == "__main__":
    main()
