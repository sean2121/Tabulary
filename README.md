# (project name TBD)

Draft a business ontology from a relational database, review it with a human in the loop, and serve it
as an Ontop virtual knowledge graph (SPARQL over the live database). Cloud-independent: everything runs
with `docker compose up`.

## What it does

1. **Scan** – reads tables, columns, keys and (optionally) column profiles from PostgreSQL (`scan/extract_pg.py`).
2. **Bootstrap** – Ontop turns every table, column and foreign key into a mechanical "direct mapping".
3. **Draft** – an LLM prunes and names the result: business names, labels, descriptions, relations,
   value-dependent subclasses (`dm_proto/dm_proto.py`).
4. **Review** – a web UI (`server/`) where a data steward approves, edits, rejects, or asks the LLM to
   change things ("position: 1 = full professor, 2 = ..."). Decisions are stored in SQLite.
5. **Export** – `v1.ttl` (OWL) and `v1.obda` (Ontop mapping); the latter converts to R2RML with
   `ontop mapping to-r2rml`.

Structure that comes from the database (tables, columns, foreign-key relations) is exported unless
rejected; unreviewed terms carry `coa:reviewStatus "unreviewed"`. Structure the LLM adds (value
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
| `COA_DATA_DIR` | `./data` | SQLite state and per-draft work files |
| `COA_CACHE_DIR` | `el_proto/cache` | LLM response cache |
| `DEMO_DB_HOST` / `DEMO_DB_PORT` | `localhost` / `5433` | where the demo Postgres is reachable |
| `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | | LLM providers |

## Layout

```
server/      FastAPI app: connections, drafts, review UI, "ask the LLM" edits
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

## Third-party material

- Insurance schema and sample data: [datadotworld/cwd-benchmark-data](https://github.com/datadotworld/cwd-benchmark-data), Apache-2.0.
- University dataset: [ontop/ontop-tutorial](https://github.com/ontop/ontop-tutorial), downloaded at runtime, not bundled.
- [Ontop](https://ontop-vkg.org/) (Apache-2.0) is downloaded into the container image.
- `server/static/`: [Pico CSS](https://picocss.com/) 2.0.6 (MIT), [htmx](https://htmx.org/) 2.0.4 (0BSD).
- Optional distractor vocabulary for `el_proto`: download schema.org's Turtle file yourself (CC BY-SA 3.0); it is not bundled.
