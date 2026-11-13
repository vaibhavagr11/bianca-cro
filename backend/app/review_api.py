"""Founder review REST API and a stateless Streamable HTTP MCP connector.

Ingestion and proposal generation use the existing n8n credential. Founder decisions
use separate, individually assigned bearer tokens. No secrets go in an artifact.
"""

import hmac
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator
from psycopg.rows import dict_row
from starlette.concurrency import run_in_threadpool

from app import brief_api, db, review_service
from app.config import settings
from app.ingest import _workspace_id, require_token
from app.pipeline_freshness import require_fresh_pipeline

log = logging.getLogger("bianca_cro.review")
router = APIRouter(prefix="/review", tags=["Sheet review"])
publish_router = APIRouter(prefix="/process", tags=["Sheet review"])
ARTIFACT_DIR = Path(__file__).resolve().parents[2] / "artifacts"
PROTOCOLS = ("2025-06-18", "2025-03-26")
ACTORS = {"RG", "NC", "NDL"}


def enabled() -> None:
    if not settings.review_enabled:
        raise HTTPException(404, "Sales Pipeline Updates is not enabled")


def validate_origin(request: Request) -> None:
    """MCP callers without Origin are servers; present origins must be allowlisted."""
    origin = request.headers.get("origin")
    if origin:
        base = urlsplit(settings.public_base_url)
        allowed = set(settings.review_allowed_origins) | {f"{base.scheme}://{base.netloc}"}
        if origin not in allowed:
            raise HTTPException(403, "Origin is not allowed")


def reviewer(request: Request, authorization: str | None = Header(default=None)) -> str:
    enabled()
    validate_origin(request)
    tokens = settings.review_tokens
    values = [v.get_secret_value() for v in tokens.values()]
    if (not tokens or set(tokens) - ACTORS or len(set(values)) != len(values)
            or any(len(v) < 32 for v in values)):
        raise HTTPException(503, "Individual founder review credentials are not configured")
    scheme, _, supplied = (authorization or "").partition(" ")
    matches = [code for code, token in tokens.items()
               if hmac.compare_digest(token.get_secret_value(), supplied)]
    if scheme.lower() != "bearer" or not supplied or not matches:
        raise HTTPException(401, "A founder review credential is required", headers={"WWW-Authenticate": "Bearer"})
    return matches[0]


Bullet = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Key = Annotated[str, StringConstraints(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class IgnoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    idempotency_key: Key


class ApproveRequest(IgnoreRequest):
    # Both destinations are explicit so an older flat-bullet client cannot silently
    # send customer context to the action column after the routing upgrade.
    next_step_bullets: list[Bullet] = Field(max_length=200)
    client_context_bullets: list[Bullet] = Field(max_length=200)

    @model_validator(mode="after")
    def cell_size(self):
        if not self.next_step_bullets and not self.client_context_bullets:
            raise ValueError("Approve at least one Next Step or Client Context bullet")
        for bullets in (self.next_step_bullets, self.client_context_bullets):
            if len("\n".join("- " + b for b in bullets)) > 45000:
                raise ValueError("The approved update is too long for one sheet cell")
        return self


class ApproveTool(ApproveRequest):
    proposal_id: UUID


class IgnoreTool(IgnoreRequest):
    proposal_id: UUID


def _workspace() -> dict:
    try:
        with db.connect() as conn:
            workspace_id = _workspace_id(conn)
            conn.row_factory = dict_row
            return review_service.workspace_view(conn, workspace_id)
    except review_service.ReviewUnavailable as exc:
        raise HTTPException(503, str(exc), headers={"Cache-Control": "no-store"}) from exc


def _build(force: bool = False) -> dict:
    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        conn.row_factory = dict_row
        return review_service.build_proposals(conn, workspace_id, force=force)


def _daily_ready() -> None:
    """Do not freeze a daily review cycle before upstream processing has finished."""
    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        row = conn.execute(
            "select d.stage, exists (select 1 from source_records s where s.workspace_id = d.workspace_id"
            " and s.status = 'pending') from daily_runs d where d.workspace_id = %s and d.for_date ="
            " (now() at time zone (select timezone from workspaces where id = %s))::date",
            (workspace_id, workspace_id)).fetchone()
    if not row or row[0] != "done" or row[1]:
        raise HTTPException(409, "Finish today's ingestion and /process/daily with stage=done before publishing review updates")


def _decision(proposal_id: str, actor: str, action: str, payload: IgnoreRequest) -> dict:
    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
    try:
        return review_service.decide(workspace_id, proposal_id, actor, action,
                                     payload.revision, payload.idempotency_key,
                                     payload.next_step_bullets if isinstance(payload, ApproveRequest) else None,
                                     payload.client_context_bullets if isinstance(payload, ApproveRequest) else None)
    except review_service.ReviewNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except review_service.ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except review_service.ReviewUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/workspace")
def workspace(response: Response, actor: str = Depends(reviewer)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {**_workspace(), "viewer": actor}


@router.post("/build")
def build(force: bool = False, x_ingest_token: str | None = Header(default=None)) -> dict:
    enabled()
    require_token(x_ingest_token)
    return _build(force=force)


@router.post("/daily")
def daily_review(x_ingest_token: str | None = Header(default=None),
                 pipeline_sync_run_id: UUID | None = None) -> dict:
    """Daily scheduler entry point; never builds or sends a second email brief."""
    enabled()
    require_token(x_ingest_token)
    if pipeline_sync_run_id is not None:
        require_fresh_pipeline(pipeline_sync_run_id)
    _daily_ready()
    return _build()


@router.post("/proposals/{proposal_id}/approve")
def approve(proposal_id: UUID, payload: ApproveRequest, actor: str = Depends(reviewer)) -> dict:
    return _decision(str(proposal_id), actor, "approve", payload)


@router.post("/proposals/{proposal_id}/ignore")
def ignore(proposal_id: UUID, payload: IgnoreRequest, actor: str = Depends(reviewer)) -> dict:
    return _decision(str(proposal_id), actor, "ignore", payload)


@router.get("/app", response_class=FileResponse)
def application() -> FileResponse:
    enabled()
    # Only the shell is public. Every data and decision call requires its own credential.
    return FileResponse(ARTIFACT_DIR / "sales-pipeline-updates.html", media_type="text/html",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer"})


@router.get("/demo", response_class=FileResponse)
def demo() -> FileResponse:
    enabled()
    return FileResponse(ARTIFACT_DIR / "sales-pipeline-updates-demo.html", media_type="text/html",
                        headers={"Cache-Control": "no-store"})


@publish_router.post("/publish")
def publish(version: Literal["v1", "v2"] = "v2", x_ingest_token: str | None = Header(default=None),
            pipeline_sync_run_id: UUID | None = None) -> dict:
    """n8n calls after daily succeeds. Build brief and review queue independently in parallel.

    This returns email content; n8n still sends the email. Repeated calls build another
    brief, matching /brief/build. Review generation itself is idempotent.
    """
    enabled()
    require_token(x_ingest_token)
    if pipeline_sync_run_id is not None:
        require_fresh_pipeline(pipeline_sync_run_id)
    _daily_ready()
    result = {}
    pipeline_args = {"pipeline_sync_run_id": pipeline_sync_run_id} if pipeline_sync_run_id is not None else {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = {"brief": pool.submit(brief_api.build_brief, False, version, x_ingest_token, **pipeline_args),
                 "review": pool.submit(_build)}
        for name, future in tasks.items():
            try:
                result[name] = {"ok": True, "result": future.result()}
            except Exception as exc:
                log.error("publish %s failed: %s", name, type(exc).__name__)
                result[name] = {"ok": False, "error": f"{name} generation failed; retry that endpoint"}
    return result


def _tools() -> list[dict]:
    return [
        {"name": "bianca_review_workspace", "description": "Read pending company sales updates and the review activity. Never approve an update without the founder's explicit decision.",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
         "annotations": {"readOnlyHint": True, "openWorldHint": False}},
        {"name": "bianca_approve_update", "description": "On the founder's Approve or Save & approve action, write next steps, commitments, and blockers to Next step, rotating its previous value into Last progress only when action bullets are approved. Write customer needs, buying signals, and objections to Client Context using the configured context policy. Empty sections leave their cells unchanged. Retry using the same idempotency key and both bullet lists.",
         "inputSchema": ApproveTool.model_json_schema(),
         "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}},
        {"name": "bianca_ignore_update", "description": "On the founder's Ignore action, dismiss this proposal without changing any Google Sheet cell.",
         "inputSchema": IgnoreTool.model_json_schema(),
         "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    ]


def _tool_call(name: str, arguments: dict, actor: str) -> dict:
    if name == "bianca_review_workspace":
        if arguments:
            raise ValueError("The workspace tool takes no arguments")
        return {**_workspace(), "viewer": actor}
    model = {"bianca_approve_update": ApproveTool, "bianca_ignore_update": IgnoreTool}.get(name)
    if not model:
        raise KeyError(name)
    data = model.model_validate(arguments)
    return _decision(str(data.proposal_id), actor, "approve" if isinstance(data, ApproveTool) else "ignore", data)


def _rpc_error(request_id, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


@router.get("/mcp")
def mcp_get(actor: str = Depends(reviewer)) -> Response:
    # Stateless JSON responses; unsolicited SSE streams are optional in MCP.
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp")
async def mcp(request: Request, actor: str = Depends(reviewer)) -> Response:
    protocol = request.headers.get("mcp-protocol-version")
    if protocol and protocol not in PROTOCOLS:
        raise HTTPException(400, "Unsupported MCP protocol version")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1_000_000:
            raise HTTPException(413, "MCP request is too large")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        return _rpc_error(None, -32700, "Invalid JSON")
    if not isinstance(data, dict) or data.get("jsonrpc") != "2.0" or not isinstance(data.get("method"), str):
        return _rpc_error(None, -32600, "Invalid JSON-RPC request")
    request_id, method, params = data.get("id"), data["method"], data.get("params", {})
    if not isinstance(params, dict) or (request_id is not None and (isinstance(request_id, bool) or not isinstance(request_id, (str, int)))):
        return _rpc_error(None, -32600, "Invalid request id or params")
    if "id" not in data:
        if method in ("notifications/initialized", "notifications/cancelled"):
            return Response(status_code=202)
        raise HTTPException(400, "Unsupported notification")
    if method == "initialize":
        requested = params.get("protocolVersion")
        result = {"protocolVersion": requested if requested in PROTOCOLS else PROTOCOLS[0],
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "Bianca Sales Pipeline Updates", "version": "3.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _tools()}
    elif method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _rpc_error(request_id, -32602, "Invalid tool call")
        if name not in {t["name"] for t in _tools()}:
            return _rpc_error(request_id, -32602, "Unknown tool")
        try:
            value = await run_in_threadpool(_tool_call, name, arguments, actor)
            payload = jsonable_encoder(value)
            result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                      "structuredContent": payload, "isError": False}
        except (ValidationError, ValueError):
            return _rpc_error(request_id, -32602, "Invalid tool arguments")
        except HTTPException as exc:
            result = {"content": [{"type": "text", "text": str(exc.detail)}],
                      "structuredContent": {"status": exc.status_code, "detail": str(exc.detail)}, "isError": True}
        except Exception as exc:
            log.error("review tool failed: %s", type(exc).__name__)
            result = {"content": [{"type": "text", "text": "The review service is unavailable; retry with the same request key."}],
                      "structuredContent": {"status": 503, "detail": "The review service is unavailable; retry with the same request key."}, "isError": True}
    else:
        return _rpc_error(request_id, -32601, "Method not found")
    return JSONResponse(jsonable_encoder({"jsonrpc": "2.0", "id": request_id, "result": result}),
                        headers={"Cache-Control": "no-store"})
