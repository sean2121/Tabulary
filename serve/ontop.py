"""A SPARQL endpoint for one draft, run by Ontop as a child process.

`ontop endpoint` loads the draft's exported ontology (v1.ttl) and mapping (v1.obda) and translates every SPARQL
query into SQL against the source database; nothing is copied. Start-up takes 15-30 s (JVM), so the process is
kept running and reused. Only one endpoint runs at a time (memory); asking for another draft, or for a draft whose
review decisions changed since start-up, stops the old process and starts a new one.

The database password reaches Ontop through the ONTOP_DB_PASSWORD environment variable of the child process,
never through a file or the command line.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("TABULARY_DATA_DIR", str(ROOT / "data")))


def ontop_home() -> Path:  # read late, after .env has been loaded
    return Path(os.environ.get("ONTOP_HOME", "/opt/ontop"))
START_TIMEOUT_S = 120
XSD = "http://www.w3.org/2001/XMLSchema#"


def jdbc_url(conninfo: str) -> str:
    kv = dict(p.split("=", 1) for p in shlex.split(conninfo) if "=" in p)
    return f"jdbc:postgresql://{kv.get('host', 'localhost')}:{kv.get('port', '5432')}/{kv.get('dbname', kv.get('user', ''))}"


def db_user(conninfo: str) -> str:
    kv = dict(p.split("=", 1) for p in shlex.split(conninfo) if "=" in p)
    return kv.get("user", "")


def _kill_stale() -> None:
    """Endpoints left behind by an earlier server process (a dev reload, a crash) are found through their pid files."""
    for pidfile in (DATA_DIR / "serve").glob("*/endpoint.pid"):
        try:
            pid = int(pidfile.read_text().strip())
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
            if b"ontop" in cmd and (_current is None or pid != _current.proc.pid):
                os.kill(pid, 15)
        except (OSError, ValueError):
            pass
        finally:
            if _current is None or pidfile.parent.name != _current.draft_id:
                pidfile.unlink(missing_ok=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Endpoint:
    draft_id: str
    stamp: str
    port: int
    proc: subprocess.Popen

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/sparql"

    def alive(self) -> bool:
        return self.proc.poll() is None


_current: Endpoint | None = None
_lock = threading.Lock()


def _wait_ready(url: str, proc: subprocess.Popen, log: Path) -> None:
    deadline = time.time() + START_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Ontop endpoint exited during start-up:\n" + _tail(log))
        try:
            with urllib.request.urlopen(url + "?query=" + urllib.parse.quote("ASK { ?s ?p ?o }"), timeout=5) as r:
                if r.status == 200:
                    return
        except urllib.error.HTTPError:
            return  # Tomcat answers: the OBDA system is loaded (Ontop starts the web server last)
        except Exception:  # noqa: BLE001  (not up yet)
            time.sleep(1)
    proc.kill()
    raise RuntimeError("Ontop endpoint did not answer within %ds:\n%s" % (START_TIMEOUT_S, _tail(log)))


def _tail(log: Path, n: int = 15) -> str:
    try:
        return "\n".join(log.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def ensure(draft_id: str, stamp: str, conninfo: str, password: str | None, ttl: str, obda: str) -> str:
    """Return the SPARQL URL of a running endpoint for this draft version, starting (or restarting) it if needed."""
    global _current
    with _lock:
        if _current and _current.draft_id == draft_id and _current.stamp == stamp and _current.alive():
            return _current.url
        stop()
        _kill_stale()
        home = ontop_home()
        if not (home / "ontop").exists():
            raise RuntimeError(f"Ontop CLI not found in {home}; set ONTOP_HOME")
        wd = DATA_DIR / "serve" / draft_id
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "v1.ttl").write_text(ttl)
        (wd / "v1.obda").write_text(obda)
        port = _free_port()
        log = wd / "endpoint.log"
        env = {**os.environ}
        env.pop("PGPASSWORD", None)
        if password is not None:
            env["ONTOP_DB_PASSWORD"] = password
        cmd = ["bash", "./ontop", "endpoint", "-m", str(wd / "v1.obda"), "-t", str(wd / "v1.ttl"),
               "--db-url", jdbc_url(conninfo), "--db-user", db_user(conninfo), "--db-driver", "org.postgresql.Driver",
               "--port", str(port), "--disable-portal-page"]
        with log.open("w") as lf:
            proc = subprocess.Popen(cmd, cwd=home, env=env, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        (wd / "endpoint.pid").write_text(str(proc.pid))
        ep = Endpoint(draft_id, stamp, port, proc)
        _wait_ready(ep.url, proc, log)
        _current = ep
        return ep.url


def stop() -> None:
    global _current
    if _current:
        if _current.alive():
            _current.proc.terminate()
            try:
                _current.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _current.proc.kill()
        _current = None


# ──────────────────────────────── queries ────────────────────────────────

UPDATE_KW = re.compile(r"\b(insert|delete|load|clear|drop|create|add|move|copy|service)\b", re.IGNORECASE)


def _strip_comments(q: str) -> str:
    """Drop # comments, but not a # inside an IRI (<...#>) or a string literal."""
    out, i, n = [], 0, len(q)
    in_iri = in_str = None
    while i < n:
        ch = q[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(q[i + 1]); i += 1
            elif ch == in_str:
                in_str = None
        elif in_iri:
            out.append(ch)
            if ch == ">":
                in_iri = None
        elif ch in "\"'":
            in_str = ch; out.append(ch)
        elif ch == "<" and re.match(r"<[^<>\s]*>", q[i:]):
            in_iri = True; out.append(ch)
        elif ch == "#":
            while i < n and q[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def check(query: str) -> tuple[str, str, str]:
    """Split a SPARQL query into (prefix block, form, body-with-form). Only SELECT and ASK are allowed."""
    q = _strip_comments(query).strip()
    if not q:
        raise ValueError("empty query")
    decl = re.compile(r"(?i)\b(PREFIX\s+[^\s<]*\s*<[^>]*>|BASE\s*<[^>]*>)")  # anywhere, not only on their own line
    prefixes = "\n".join(m.group(0) for m in decl.finditer(q))
    body = decl.sub("", q).strip()
    form = body.split(None, 1)[0].upper() if body else ""
    if form not in ("SELECT", "ASK"):
        raise ValueError("only SELECT and ASK queries are allowed")
    if UPDATE_KW.search(re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|<[^>]*>', '""', body)):
        raise ValueError("update operations and SERVICE are not allowed")
    return prefixes, form, body


def run(url: str, query: str, limit: int = 100, timeout_s: int = 60) -> dict:
    prefixes, form, body = check(query)
    limit = max(1, min(int(limit), 1000))
    final = f"{prefixes}\n{body}" if form == "ASK" else f"{prefixes}\nSELECT * WHERE {{ {body} }} LIMIT {limit + 1}"
    data = urllib.parse.urlencode({"query": final}).encode()
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/sparql-results+json",
                                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            res = json.loads(r.read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")
        raise ValueError("SPARQL endpoint rejected the query: " + re.sub(r"\s+", " ", msg)[:800])
    if form == "ASK":
        return {"ask": bool(res.get("boolean")), "columns": [], "rows": [], "truncated": False}
    cols = res["head"]["vars"]
    rows = [[_term(b.get(v)) for v in cols] for b in res["results"]["bindings"]]
    return {"columns": cols, "rows": rows[:limit], "truncated": len(rows) > limit}


def _term(t: dict | None) -> str:
    if t is None:
        return ""
    v = t["value"]
    if t["type"] == "uri":
        return "<" + v + ">"
    return v


def to_markdown(res: dict, limit: int, prefixes: dict[str, str] | None = None) -> str:
    if "ask" in res:
        return "true" if res["ask"] else "false"
    cols, rows = res["columns"], res["rows"]
    if not rows:
        return "| " + " | ".join(cols) + " |\n(0 results)"

    def short(v: str) -> str:
        if v.startswith("<") and prefixes:
            for p, ns in prefixes.items():
                if v[1:].startswith(ns):
                    return f"{p}:{v[1 + len(ns):-1]}"
        return v.replace("|", "\\|").replace("\n", " ")
    L = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        L.append("| " + " | ".join(short(c) for c in r) + " |")
    L.append(f"({len(rows)} results" + (f", truncated at limit {limit}; aggregate or filter instead of raising the limit" if res["truncated"] else "") + ")")
    return "\n".join(L)


atexit.register(stop)
