# Tabulary

Tables in, vocabulary out.

Draft a business ontology from a relational database, review it with a human in the loop, and serve it
as an Ontop virtual knowledge graph (SPARQL over the live database). Cloud-independent: everything runs
with `docker compose up`.

## What it does

1. **Scan** – reads tables, columns, keys and (optionally) column profiles from PostgreSQL (`scan/extract_pg.py`).
2. **Bootstrap** – Ontop turns every table, column and foreign key into a mechanical "direct mapping".
3. **Draft** – an LLM prunes and names the result: business names, labels, descriptions, relations,
   value-dependent subclasses (`dm_proto/dm_proto.py`).
4. **Review** – a web UI (`server/`) where a data steward approves, edits, rejects, or asks the LLM to
   change things ("position: 1 = full professor, 2 = ...", "this column refers to the policy, add the
   relation"): renames, value subclasses, and relations the database has no foreign key for. Decisions
   are stored in SQLite.
5. **Export** – `v1.ttl` (OWL) and `v1.obda` (Ontop mapping); the latter converts to R2RML with
   `ontop mapping to-r2rml`.
6. **Serve** – an MCP server (`serve/`) that gives an LLM client the reviewed vocabulary as context and
   a read-only SQL tool, so it can answer business questions over the database (see below).

Structure that comes from the database (tables, columns, foreign-key relations) is exported unless
rejected; unreviewed terms carry `tabulary:reviewStatus "unreviewed"`. Structure the LLM adds (value
subclasses, superclasses) is exported only after approval.

## Run

```sh
cp .env.example .env          # add an LLM key (DeepSeek or OpenAI) or point to a local Ollama
docker compose up --build
open http://localhost:8000/
```

Two demo databases are registered on first start:

- **Insurance demo** – P&C schema from the data.world CWD benchmark (bundled, `db/init/01_insurance.sql`).
- **University demo** – the Ontop tutorial dataset, downloaded from the Ontop repository on first start
  (`docker/load_demo.sh`; set `LOAD_DEMO=0` to skip).

### Without Docker

```sh
pip install -r requirements.txt
# needs Java 17+, psql, and the Ontop CLI with the PostgreSQL JDBC driver in its jdbc/ folder
ONTOP_HOME=/path/to/ontop-cli uvicorn server.app:app --port 8000
```

| Variable | Default | Meaning |
|---|---|---|
| `ONTOP_HOME` | `/opt/ontop` | directory containing the `ontop` launcher |
| `TABULARY_DATA_DIR` | `./data` | SQLite state and per-draft work files |
| `TABULARY_CACHE_DIR` | `el_proto/cache` | LLM response cache |
| `DEMO_DB_HOST` / `DEMO_DB_PORT` | `localhost` / `5433` | where the demo Postgres is reachable |
| `DEMO_DB_PASSWORD` | `tabulary` | password of the demo Postgres |
| `TABULARY_PW_*` (any name) | | passwords of your own databases; a saved connection stores the variable name, `.env` the value |
| `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | | LLM providers |

## Serve the result to an LLM (MCP)

`serve/mcp_server.py` is a Model Context Protocol server over stdio (plain JSON-RPC, no SDK). It exposes
one finished draft as three tools:

| tool | what it does |
|---|---|
| `describe_schema` | the reviewed vocabulary. Default: classes and properties with the prefixes to use in SPARQL. `mode=sql`: the SQL table, column and JOIN condition behind each term; unreviewed items are marked |
| `sparql` | SPARQL SELECT/ASK over the vocabulary. An `ontop endpoint` child process translates it to SQL with the reviewed mapping, so only mapped terms are reachable; starts on first use (20-30 s) and restarts when review decisions change |
| `query_sql` | fallback: one read-only `SELECT` for what SPARQL cannot express (window functions, DB-specific functions): statement check, `READ ONLY` transaction, timeout, row cap |
| `distinct_values` | values of one column with counts, for decoding codes |

Drafts do not store database passwords, so the connection is passed separately (a connection saved in
the UI, a libpq string, or `TABULARY_CONNINFO`). Passwords never go on the command line: a saved connection
names an environment variable, the server reads `.env` at start-up, and psql gets the value as `PGPASSWORD`.
Register it with Claude Code, for example:

```sh
# the running stack (Docker or uvicorn) serves MCP over HTTP: latest finished draft, or /mcp/<draft id>
claude mcp add --transport http tabulary http://localhost:8000/mcp

# or run it as a stdio process without the web app
claude mcp add tabulary -- python3 /path/to/tabulary/serve/mcp_server.py \
  --draft <draft id> --connection <saved connection id>
```

Over HTTP the database is the saved connection the draft was created from. Neither the web UI nor the MCP
endpoint has authentication; put an authenticating reverse proxy in front before exposing them beyond a trusted
network. Then ask: "Which policyholders have claims, and what is the total claim amount per policyholder?"
The client calls `describe_schema`, writes SPARQL with the terms it returns and runs it through `sparql`; Ontop turns that into SQL. For computations SPARQL lacks, it falls back to `query_sql`.
Without `--draft` the latest finished draft is used.

## Layout

```
server/      FastAPI app: connections, drafts, review UI, "ask the LLM" edits
serve/       MCP server: reviewed vocabulary as context + read-only SQL for LLM clients
scan/        PostgreSQL catalog extraction (information_schema + profiles)
dm_proto/    draft pipeline: direct mapping -> LLM pruning/naming -> OWL + OBDA
el_proto/    earlier prototype: linking a schema to an existing ontology (shared helpers live here)
bench/       benchmark inputs and scorer (insurance demo vs. the data.world R2RML gold)
db/init/     SQL loaded into the demo Postgres on first start
docker/      container helpers
```

## Benchmark

`python3 dm_proto/dm_proto.py --provider deepseek --stats-level values` scores the insurance draft
against the gold set derived from data.world's R2RML (`el_proto/gold_r2rml.yaml`). The gold treats
every column the R2RML does not touch as "none", so only the lenient scores (identifier columns and
subtype links may be dropped) are meaningful.

## License

Apache-2.0 (see LICENSE).

## Third-party material

- Insurance schema and sample data: [datadotworld/cwd-benchmark-data](https://github.com/datadotworld/cwd-benchmark-data), Apache-2.0.
- University dataset: [ontop/ontop-tutorial](https://github.com/ontop/ontop-tutorial), downloaded at runtime, not bundled.
- [Ontop](https://ontop-vkg.org/) (Apache-2.0) is downloaded into the container image.
- `server/static/`: [Pico CSS](https://picocss.com/) 2.0.6 (MIT), [htmx](https://htmx.org/) 2.0.4 (0BSD).
- Optional distractor vocabulary for `el_proto`: download schema.org's Turtle file yourself (CC BY-SA 3.0); it is not bundled.
