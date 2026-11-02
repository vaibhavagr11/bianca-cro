"""Mark as complete: the brief's buttons link here with a signed token; a confirm page asks who you are, then records it.

Email can't post forms and mail scanners open links, so the link only shows a confirm page (GET changes nothing);
the change happens on the page's POST. Tokens carry what the page shows (deal, text) and expire after LINK_DAYS.
"""

import base64
import hashlib
import hmac
import html
import json
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from app import db
from app.config import settings
from app.ingest import _workspace_id

router = APIRouter(prefix="/actions")
LINK_DAYS = 14
KINDS = ("insight", "thread", "rec")
WHO = {"RG": "Ramiro", "NC": "Nico", "NDL": "Nicolás de León"}
WHAT = {"insight": "It won't appear in the next brief.",
        "thread": "This email won't show as waiting on a reply until the customer writes again.",
        "rec": "This suggestion won't be shown again."}


def _key() -> bytes:
    if settings.brief_action_secret is None:
        raise HTTPException(status_code=503, detail="Mark as complete is not configured")
    return settings.brief_action_secret.get_secret_value().encode()


def _mac(body: str) -> str:
    return hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:32]


def sign(ref: str, deal: str, text: str, now: float | None = None) -> str:
    """Token for one row: 'kind:id' plus what the confirm page shows, valid for LINK_DAYS."""
    data = {"r": ref, "d": deal, "x": text[:120], "e": int((time.time() if now is None else now) + LINK_DAYS * 86400)}
    body = base64.urlsafe_b64encode(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
    body_s = body.decode().rstrip("=")
    return f"{body_s}.{_mac(body_s)}"


def verify(token: str, now: float | None = None) -> dict:
    body, _, mac = token.rpartition(".")
    if not body or not hmac.compare_digest(mac, _mac(body)):
        raise HTTPException(status_code=400, detail="This link isn't valid.")
    data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if data["e"] < (time.time() if now is None else now):
        raise HTTPException(status_code=410, detail="This link has expired: it's from an older brief.")
    kind, _, ref = data["r"].partition(":")
    if kind not in KINDS or not ref:
        raise HTTPException(status_code=400, detail="This link isn't valid.")
    return {"kind": kind, "ref": ref, "deal": data["d"], "text": data["x"]}


def link(ref: str, deal: str, text: str) -> str:
    return f"{settings.public_base_url}/actions/complete?t={sign(ref, deal, text)}"


def apply(conn, workspace_id, kind: str, ref: str, action: str, actor: str) -> None:
    """Record a click; for an insight also set its status (undo only reopens what a founder closed here)."""
    conn.execute("insert into brief_actions (workspace_id, kind, ref, action, actor) values (%s, %s, %s, %s, %s)",
                 (workspace_id, kind, ref, action, actor))
    if kind != "insight":
        return
    if action == "done":
        conn.execute("update insights set status = 'done', completion = coalesce(completion, '{}'::jsonb) || %s"
                     " where workspace_id = %s and id::text = %s and status in ('open', 'possibly_done')",
                     (Jsonb({"via": "brief", "by": actor, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}),
                      workspace_id, ref))
    else:
        conn.execute("update insights set status = 'open', completion = nullif(completion - 'via' - 'by' - 'at', '{}'::jsonb)"
                     " where workspace_id = %s and id::text = %s and status = 'done' and completion->>'via' = 'brief'",
                     (workspace_id, ref))


def latest(conn, workspace_id) -> dict[tuple[str, str], tuple[str, object]]:
    """(kind, ref) -> (latest action, when); empty if the table doesn't exist yet."""
    cur = conn.cursor(row_factory=tuple_row)  # the brief's connection returns dict rows
    if cur.execute("select to_regclass('brief_actions')").fetchone()[0] is None:
        return {}
    return {(r[0], r[1]): (r[2], r[3]) for r in cur.execute(
        "select distinct on (kind, ref) kind, ref, action, created_at from brief_actions where workspace_id = %s"
        " order by kind, ref, created_at desc", (workspace_id,))}


# ---------------------------------------------------------------- pages

def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
        f'initial-scale=1"><title>{html.escape(title)}</title><style>body{{font:15px/1.5 -apple-system,Segoe UI,'
        'Arial,sans-serif;color:#101828;max-width:520px;margin:40px auto;padding:0 16px}}.item{{background:#f2f4f7;'
        'padding:12px 14px;border-radius:6px;margin:12px 0}}button{{font:inherit;padding:8px 16px;border-radius:6px;'
        'border:1px solid #d0d5dd;background:#fff;cursor:pointer;margin:4px 6px 4px 0}}button.go{{background:#175cd3;'
        'color:#fff;border-color:#175cd3}}.muted{{color:#667085;font-size:13px}}</style></head>'
        f"<body>{body}</body></html>")


def _item(a: dict) -> str:
    return f'<div class="item"><b>{html.escape(a["deal"])}</b><br>{html.escape(a["text"])}</div>'


@router.get("/complete", response_class=HTMLResponse)
def confirm_page(t: str) -> HTMLResponse:
    a = verify(t)
    who = "".join(f'<button class="go" name="who" value="{c}">I\'m {html.escape(n)}</button>' for c, n in WHO.items())
    return _page("Mark as complete", f"<h2>Mark as complete?</h2>{_item(a)}"
                 f'<form method="post" action="/actions/complete"><input type="hidden" name="t" value="{html.escape(t)}">'
                 f'{who}</form><p class="muted">{WHAT[a["kind"]]} You can undo it on the next page.</p>')


async def _form(request: Request) -> tuple[dict, str, str]:
    """The confirm page's two fields (plain urlencoded form; no extra dependency)."""
    fields = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    t, who = fields.get("t", ""), fields.get("who", "")
    if who not in WHO:
        raise HTTPException(status_code=400, detail="Pick who you are.")
    return verify(t), t, who


@router.post("/complete", response_class=HTMLResponse)
async def complete(request: Request) -> HTMLResponse:
    a, t, who = await _form(request)
    with db.connect() as conn:
        apply(conn, _workspace_id(conn), a["kind"], a["ref"], "done", who)
    return _page("Marked complete", f"<h2>Marked complete ✓</h2>{_item(a)}<p>{WHAT[a['kind']]}</p>"
                 f'<form method="post" action="/actions/undo"><input type="hidden" name="t" value="{html.escape(t)}">'
                 f'<input type="hidden" name="who" value="{who}"><button>Undo</button></form>')


@router.post("/undo", response_class=HTMLResponse)
async def undo(request: Request) -> HTMLResponse:
    a, t, who = await _form(request)
    with db.connect() as conn:
        apply(conn, _workspace_id(conn), a["kind"], a["ref"], "undone", who)
    return _page("Undone", f"<h2>Undone</h2>{_item(a)}<p>It's open again and will show in the brief as before.</p>")
