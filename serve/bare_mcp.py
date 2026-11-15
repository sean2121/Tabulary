#!/usr/bin/env python3
"""Baseline MCP server for the question-answering comparison: the raw database schema and read-only SQL, no Tabulary vocabulary.

describe_schema returns what information_schema knows (tables, columns with types, primary and foreign keys) and nothing
else, which is what an LLM gets when it is pointed at a database directly.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serve import sql  # noqa: E402
from serve.mcp_server import Server, log  # noqa: E402
from server import connections as C  # noqa: E402

SCHEMA_SQL = """
SELECT c.table_name, c.column_name, c.data_type, c.is_nullable,
  EXISTS (SELECT 1 FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage k
          ON k.constraint_name = tc.constraint_name AND k.table_schema = tc.table_schema
          WHERE tc.constraint_type = 'PRIMARY KEY' AND k.table_name = c.table_name AND k.column_name = c.column_name AND tc.table_schema = c.table_schema) AS pk,
  (SELECT ccu.table_name || '.' || ccu.column_name FROM information_schema.table_constraints tc
     JOIN information_schema.key_column_usage k ON k.constraint_name = tc.constraint_name AND k.table_schema = tc.table_schema
     JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
     WHERE tc.constraint_type = 'FOREIGN KEY' AND k.table_name = c.table_name AND k.column_name = c.column_name AND tc.table_schema = c.table_schema LIMIT 1) AS fk
FROM information_schema.columns c
WHERE c.table_schema = %s
ORDER BY c.table_name, c.ordinal_position
"""


class Bare(Server):
    def __init__(self, conninfo: str, env: dict, schema: str) -> None:  # noqa: D107
        self.conninfo, self.env, self.schema = conninfo, env, schema
        log("bare schema server")

    def tools(self) -> list[dict]:
        return [
            {"name": "describe_schema",
             "description": "List the database tables and columns (types, primary keys, foreign keys). Call this first.",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "query_sql",
             "description": "Run one read-only SQL SELECT against the database (PostgreSQL).",
             "inputSchema": {"type": "object", "required": ["sql"], "properties": {
                 "sql": {"type": "string"}, "limit": {"type": "integer", "default": 100}}}},
        ]

    def call(self, name: str, args: dict) -> str:
        if name == "describe_schema":
            res = sql.run(self.conninfo, SCHEMA_SQL.replace("%s", "'" + self.schema.replace("'", "''") + "'"), self.schema, 1000, env=self.env)
            out, cur = [], None
            for t, col, typ, nullable, pk, fk in res["rows"]:
                if t != cur:
                    out.append(f"\n{t}"); cur = t
                flags = (" PK" if pk == "t" else "") + (f" FK -> {fk}" if fk else "") + ("" if nullable == "YES" else " NOT NULL")
                out.append(f"  {col} {typ}{flags}")
            return f"PostgreSQL schema {self.schema}" + "\n".join(out)
        if name == "query_sql":
            limit = int(args.get("limit") or 100)
            return sql.to_markdown(sql.run(self.conninfo, args["sql"], self.schema, limit, env=self.env), limit)
        raise ValueError(f"unknown tool {name}")

    def handle(self, msg: dict):
        out = super().handle(msg)
        if msg.get("method") == "initialize" and out:
            out["result"]["instructions"] = "Answer questions over this database. Call describe_schema, then write SQL and run it with query_sql."
        return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--connection", required=True)
    ap.add_argument("--schema", default="public")
    a = ap.parse_args()
    c = C.get(a.connection)
    Bare(C.conninfo(c), C.pg_env(c), a.schema).serve()


if __name__ == "__main__":
    main()
