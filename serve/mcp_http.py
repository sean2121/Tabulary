"""MCP over Streamable HTTP, served by the Tabulary web app itself.

POST /mcp            latest finished draft
POST /mcp/{draft}    a specific draft
The body is one JSON-RPC message (or a batch); the reply is application/json. The server is stateless (no
Mcp-Session-Id), so GET (server-initiated SSE stream) answers 405 and DELETE 200. Register in Claude Code with
  claude mcp add --transport http tabulary http://localhost:8000/mcp

The database behind a draft is the saved connection it was created from (drafts.connection_id); its password is
the environment variable that connection names, which reaches the container through docker compose's env_file.
No authentication: the web UI has none either; both are meant for a trusted network.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from serve import context
from serve.mcp_server import Server
from server import connections as C
from server import drafts as D

router = APIRouter()
_cache: dict[str, tuple[str, Server]] = {}  # draft id -> (version stamp, server)


class _NoDraft(Server):
    """Answers initialize / tools/list before any draft exists, so a client can connect to a fresh stack."""

    def __init__(self) -> None:  # noqa: D107  (no model to load)
        pass

    def call(self, name: str, args: dict) -> str:
        raise ValueError("no finished draft yet: create one in the Tabulary UI first")


def _stamp(draft_id: str) -> str:
    """Changes whenever a review decision or an applied request touches the draft, so the cached model is rebuilt."""
    with D.db() as c:
        r = c.execute("SELECT count(*), max(decided_at) FROM draft_items WHERE draft_id=?", (draft_id,)).fetchone()
    return f"{r[0]}:{r[1]}"


def server_for(draft_id: str | None) -> Server:
    did = draft_id or context.latest_done_draft_id()
    if not did:
        raise LookupError("no finished draft")
    st = _stamp(did)
    hit = _cache.get(did)
    if hit and hit[0] == st:
        return hit[1]
    info = context.draft_info(did)
    conninfo, env = None, None
    if info.get("connection_id"):
        c = C.get(info["connection_id"])
        conninfo, env = C.conninfo(c), C.pg_env(c)  # raises RuntimeError when the password variable is missing
    srv = Server(did, conninfo, env)
    _cache[did] = (st, srv)
    return srv


async def _post(request: Request, draft_id: str | None) -> Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, status_code=400)
    needs_draft = any(isinstance(m, dict) and m.get("method") == "tools/call" for m in (body if isinstance(body, list) else [body]))
    try:
        srv: Server = server_for(draft_id) if needs_draft else _NoDraft()
    except (LookupError, RuntimeError, ValueError) as e:  # no draft, unknown draft, or password variable missing
        mid = body.get("id") if isinstance(body, dict) else None
        return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}})
    msgs = body if isinstance(body, list) else [body]
    out = [r for r in (srv.handle(m) for m in msgs if isinstance(m, dict)) if r is not None]
    if not out:  # notifications only
        return Response(status_code=202)
    return JSONResponse(out if isinstance(body, list) else out[0])


@router.post("/mcp")
async def mcp_root(request: Request) -> Response:
    return await _post(request, None)


@router.post("/mcp/{draft_id}")
async def mcp_draft(request: Request, draft_id: str) -> Response:
    return await _post(request, draft_id)


@router.get("/mcp")
@router.get("/mcp/{draft_id}")
def mcp_get(draft_id: str | None = None) -> Response:
    return Response("this server does not open a server-initiated stream; POST JSON-RPC messages", status_code=405)


@router.delete("/mcp")
@router.delete("/mcp/{draft_id}")
def mcp_delete(draft_id: str | None = None) -> Response:
    return Response(status_code=200)  # stateless: nothing to end
