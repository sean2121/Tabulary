#!/usr/bin/env python3
"""Compare question answering over the demo database under three conditions, using Claude Code as the client.

  bare    raw schema (information_schema) + SQL           serve/bare_mcp.py
  sql     Tabulary vocabulary (mode=sql) + SQL            serve/mcp_server.py --tools describe_schema,query_sql,distinct_values
  sparql  Tabulary vocabulary + SPARQL through Ontop      serve/mcp_server.py --tools describe_schema,sparql,distinct_values

Each question is one `claude -p` run; the reply must end with "ANSWER: <value>", which is compared with the gold value
after normalising numbers. Small sample, one model: this shows direction, not a publishable accuracy.

Usage: python3 bench/qa/run.py --draft ID --connection ID [--conditions bare,sql,sparql] [--model sonnet] [--out results.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable

PROMPT = ("Using only the provided MCP tools, answer this question about the database: {q}\n"
          "Be concise. The last line of your reply must be exactly 'ANSWER: <value>' with just the number or identifier "
          "(no units, no thousands separators, no words). If you cannot determine it, write 'ANSWER: unknown'.")


def norm(v: str) -> str:
    v = v.strip().strip("`*'\"").replace(",", "")
    m = re.fullmatch(r"-?\d+(\.\d+)?", v)
    if m:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    return v.lower()


def server_cmd(cond: str, draft: str, conn: str) -> list[str]:
    if cond == "bare":
        return [PY, str(ROOT / "serve" / "bare_mcp.py"), "--connection", conn]
    tools = {"sql": "describe_schema,query_sql,distinct_values", "sparql": "describe_schema,sparql,distinct_values"}[cond]
    return [PY, str(ROOT / "serve" / "mcp_server.py"), "--draft", draft, "--connection", conn, "--tools", tools]


def run_one(cfg: Path, q: str, model: str, timeout: int) -> tuple[str, str, float]:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    t0 = time.time()
    r = subprocess.run(["claude", "-p", PROMPT.format(q=q), "--mcp-config", str(cfg), "--strict-mcp-config",
                        "--allowedTools", "mcp__tabulary__*", "--model", model, "--output-format", "text"],
                       capture_output=True, text=True, timeout=timeout, env=env, cwd=tempfile.gettempdir())
    out = r.stdout
    m = re.findall(r"ANSWER:\s*(.+)", out)
    return (m[-1].strip() if m else "(no answer)"), out, time.time() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True)
    ap.add_argument("--connection", required=True)
    ap.add_argument("--conditions", default="bare,sql,sparql")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--timeout", type=int, default=420)
    ap.add_argument("--out", default=str(ROOT / "bench" / "qa" / "results.json"))
    ap.add_argument("--only", help="comma-separated question ids")
    a = ap.parse_args()
    qs = yaml.safe_load((ROOT / "bench" / "qa" / "questions.yaml").read_text())
    if a.only:
        keep = set(a.only.split(","))
        qs = [x for x in qs if x["id"] in keep]
    results = []
    with tempfile.TemporaryDirectory() as td:
        for cond in a.conditions.split(","):
            cfg = Path(td) / f"{cond}.json"
            cfg.write_text(json.dumps({"mcpServers": {"tabulary": {"command": server_cmd(cond, a.draft, a.connection)[0],
                                                                    "args": server_cmd(cond, a.draft, a.connection)[1:]}}}))
            for x in qs:
                try:
                    ans, out, dt = run_one(cfg, x["q"], a.model, a.timeout)
                except subprocess.TimeoutExpired:
                    ans, out, dt = "(timeout)", "", float(a.timeout)
                ok = norm(ans) == norm(x["gold"])
                results.append({"condition": cond, "id": x["id"], "question": x["q"], "gold": x["gold"], "answer": ans, "correct": ok,
                                "seconds": round(dt, 1), "transcript": out[-6000:]})
                print(f"{cond:7} {x['id']:4} {'OK ' if ok else 'NG '} gold={x['gold']:>6} got={ans[:30]:30} {dt:5.0f}s", flush=True)
                Path(a.out).write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print()
    for cond in a.conditions.split(","):
        rs = [r for r in results if r["condition"] == cond]
        print(f"{cond:7} {sum(r['correct'] for r in rs)}/{len(rs)} correct, mean {sum(r['seconds'] for r in rs) / max(1, len(rs)):.0f}s per question")


if __name__ == "__main__":
    main()
