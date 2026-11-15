#!/usr/bin/env python3
"""Tabulary MCP server (stdio).

Exposes an approved Tabulary draft to an LLM client (Claude Code, Claude Desktop, ...) as four tools:
  describe_schema   the vocabulary: in SPARQL terms by default, or with the SQL tables/columns/JOINs behind it (mode=sql)
  sparql            SPARQL over the draft; Ontop translates it to SQL using the reviewed mapping (primary path)
  query_sql         read-only SQL for what SPARQL cannot express (window functions, DB-specific functions)
  distinct_values   values of one column with counts (for decoding codes)

The protocol is JSON-RPC 2.0, one message per line on stdin/stdout (MCP stdio transport); no SDK is needed.

Usage:
  python3 -m serve.mcp_server [--draft ID] (--connection SAVED_CONNECTION_ID | --conninfo "host=... dbname=... user=...")
  Without --draft the latest finished draft is used. Drafts do not store passwords, so the connection is passed
  separately; TABULARY_CONNINFO is used when neither flag is given. The password comes from the environment: a
  saved connection names its variable (password_env); for --conninfo use PGPASSWORD. .env in the project root is
  read at start-up, so nothing secret has to be put on the command line that the LLM client stores in its config.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serve import context, ontop, sql  # noqa: E402
from server import drafts as D  # noqa: E402
from server import env as E  # noqa: E402  (loads .env)

VERSION = "0.1"
PROTOCOL = "2025-06-18"


def log(msg: str) -> None:
    print(f"[tabulary-mcp] {msg}", file=sys.stderr, flush=True)


class Server:
    def __init__(self, draft_id: str, conninfo: str | None, env: dict | None) -> None:
        """conninfo None = no database reachable: describe_schema works, query / distinct_values explain why they do not."""
        self.draft_id = draft_id
        self.info = context.draft_info(draft_id)
        self.model, self.dm = context.load(draft_id)
        self.conninfo, self.env = conninfo, env
        self.schema = self.info.get("schema_name") or "public"
        self.tables = context.known_tables(self.dm)
        self._exports: tuple[str, str] | None = None
        log(f"draft {draft_id} ({len(self.model['classes'])} classes, schema {self.schema})")

    def exports(self) -> tuple[str, str]:
        if self._exports is None:
            self._exports = (D.export(self.draft_id, "v1.ttl"), D.export(self.draft_id, "v1.obda"))
        return self._exports

    # ---- tools ----
    def tools(self) -> list[dict]:
        return [
            {"name": "describe_schema",
             "description": "Business vocabulary of this database as reviewed by its data stewards. Default mode "
                            "'ontology' gives classes and properties with the prefixes to use in SPARQL. Mode 'sql' adds "
                            "the SQL table, column and JOIN condition behind each term, for query_sql. Call this first.",
             "inputSchema": {"type": "object", "properties": {
                 "detail": {"type": "string", "enum": ["summary", "full"], "default": "full",
                            "description": "summary = class list only; full = properties and relations"},
                 "mode": {"type": "string", "enum": ["ontology", "sql"], "default": "ontology"}}}},
            {"name": "sparql",
             "description": "Run a SPARQL 1.1 SELECT or ASK query over the reviewed vocabulary. Ontop translates it to SQL "
                            "against the live database using the stewards' mapping, so only mapped classes and properties "
                            "can be reached. Preferred way to answer questions. The first call after a change may take "
                            "20-30 s while the endpoint starts.",
             "inputSchema": {"type": "object", "required": ["query"], "properties": {
                 "query": {"type": "string", "description": "SPARQL query using the prefixes from describe_schema"},
                 "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000}}}},
            {"name": "query_sql",
             "description": "Fallback: run one read-only SQL SELECT against the database (PostgreSQL) for computations "
                            "SPARQL cannot express (window functions, date arithmetic, DB-specific functions). Use the "
                            "table and column names from describe_schema with mode='sql'.",
             "inputSchema": {"type": "object", "required": ["sql"], "properties": {
                 "sql": {"type": "string", "description": "A single SELECT or WITH ... SELECT statement"},
                 "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000}}}},
            {"name": "distinct_values",
             "description": "Distinct values of one column with row counts, most frequent first. Use it to see which "
                            "codes or categories a column actually holds.",
             "inputSchema": {"type": "object", "required": ["table", "column"], "properties": {
                 "table": {"type": "string", "description": "SQL table name as given by describe_schema"},
                 "column": {"type": "string"},
                 "limit": {"type": "integer", "default": 30, "minimum": 1, "maximum": 500}}}},
        ]

    def call(self, name: str, args: dict) -> str:
        if name == "describe_schema":
            detail = args.get("detail") or "full"
            if (args.get("mode") or "ontology") == "sql":
                return context.describe(self.model, self.dm, self.info, detail)
            return context.describe_ontology(self.model, self.dm, self.info, detail)
        if name in ("sparql", "query_sql", "distinct_values") and not self.conninfo:
            raise ValueError("no database connection for this draft (it was created from a raw connection string; "
                             "create the draft from a saved connection, or start the stdio server with --connection)")
        if name == "sparql":
            limit = int(args.get("limit") or 100)
            ttl, obda = self.exports()
            url = ontop.ensure(self.draft_id, context.draft_stamp(self.draft_id), self.conninfo,
                               (self.env or {}).get("PGPASSWORD"), ttl, obda)
            res = ontop.run(url, args["query"], limit)
            return ontop.to_markdown(res, limit, context.prefixes(self.model))
        if name == "query_sql":
            limit = int(args.get("limit") or 100)
            res = sql.run(self.conninfo, args["sql"], self.schema, limit, env=self.env)
            return sql.to_markdown(res, limit)
        if name == "distinct_values":
            table, column = args["table"].strip().strip('"'), args["column"].strip().strip('"')
            tu = next((tu for tu in self.dm if context._tbl(self.dm, tu).lower() == table.lower()), None)
            if tu is None:
                raise ValueError(f"unknown table {table!r}; known: {', '.join(sorted(self.tables))}")
            cols = context.known_columns(self.dm, tu)
            col = next((c for c in cols if c.lower() == column.lower()), None)
            if col is None:
                raise ValueError(f"unknown column {column!r} in {table}; columns: {', '.join(cols)}")
            limit = int(args.get("limit") or 30)
            q = f'SELECT "{col}" AS value, count(*) AS rows FROM {self.dm[tu]["sql_name"]} GROUP BY 1 ORDER BY 2 DESC, 1'
            return sql.to_markdown(sql.run(self.conninfo, q, self.schema, limit, env=self.env), limit)
        raise ValueError(f"unknown tool {name}")

    # ---- JSON-RPC ----
    def handle(self, msg: dict) -> dict | None:
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            return self.ok(mid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                                 "capabilities": {"tools": {}},
                                 "serverInfo": {"name": "tabulary", "version": VERSION},
                                 "instructions": "Answer business questions over this database. Call describe_schema first, "
                                                 "write SPARQL with the prefixes and terms it gives and run it with sparql. "
                                                 "Use query_sql (with describe_schema mode='sql') only for computations "
                                                 "SPARQL cannot express. Always show the query you ran."})
        if method in ("notifications/initialized", "notifications/cancelled") or mid is None:
            return None
        if method == "ping":
            return self.ok(mid, {})
        if method == "tools/list":
            return self.ok(mid, {"tools": self.tools()})
        if method in ("resources/list", "prompts/list"):
            return self.ok(mid, {method.split("/")[0]: []})
        if method == "tools/call":
            try:
                text = self.call(params.get("name", ""), params.get("arguments") or {})
                return self.ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as e:  # report tool failures as results, so the model can correct itself
                return self.ok(mid, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}

    @staticmethod
    def ok(mid, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log(f"bad json: {line[:80]}")
                continue
            out = self.handle(msg)
            if out is not None:
                sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
                sys.stdout.flush()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--draft", help="draft id (default: latest finished draft)")
    ap.add_argument("--connection", help="id of a connection saved in the Tabulary UI")
    ap.add_argument("--conninfo", help='libpq connection string without password, e.g. "host=localhost port=5433 dbname=tabulary user=tabulary"; set PGPASSWORD')
    a = ap.parse_args()
    draft_id = a.draft or context.latest_done_draft_id()
    if not draft_id:
        sys.exit("no finished draft found")
    conninfo, pw = E.split_password(a.conninfo or os.environ.get("TABULARY_CONNINFO") or "")
    env = E.pg_env(pw)
    if a.connection:
        from server import connections as C
        c = C.get(a.connection)
        try:
            conninfo, env = C.conninfo(c), C.pg_env(c)
        except RuntimeError as e:
            sys.exit(str(e))
    if not conninfo:
        sys.exit("give --connection, --conninfo or TABULARY_CONNINFO")
    Server(draft_id, conninfo, env).serve()


if __name__ == "__main__":
    main()
