"""The HTTP surface. Thin on purpose — the work lives in the modules beside it."""

from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.responses import Response as RawResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from db import models as db
from db.models import AppUser
from db.session import SessionLocal, get_session

from . import (
    auth,
    binding,
    datasources,
    events,
    health,
    jobs,
    pages,
    preview,
    printers,
    retention,
    sites,
    zplimport,
)
from .models import Template
from .settings import settings
from .zpl import RenderError, render_run, separator

# Left to a cron job, nobody sets one up, and the labels pile up until
# somebody notices the disk. Once a day, whichever process gets there first.
HOUSEKEEPING_EVERY = 3600.0


def _housekeeping(stop: threading.Event) -> None:
    while not stop.wait(HOUSEKEEPING_EVERY):
        try:
            with SessionLocal() as s:
                if retention.due(s):
                    retention.prune(s)
        except Exception:
            log.exception("housekeeping failed; trying again in an hour")


def _checkups(stop: threading.Event) -> None:
    """Ask every printer how it is, every half minute, so a screen can say
    "out of labels" without anyone pressing Re-check."""
    while True:
        try:
            with SessionLocal() as s:
                health.check_all(s)
        except Exception:
            log.exception("printer health check failed; trying again shortly")
        if stop.wait(health.EVERY):
            return


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    auth.bootstrap()
    stop = threading.Event()
    threading.Thread(target=_housekeeping, args=(stop,), daemon=True,
                     name="platen-housekeeping").start()
    threading.Thread(target=_checkups, args=(stop,), daemon=True,
                     name="platen-health").start()
    yield
    stop.set()


log = logging.getLogger("platen.api")

app = FastAPI(title="Platen", lifespan=lifespan)

WEB = Path(__file__).resolve().parent.parent / "web"


def audit(s: Session, action: str, entity: str, entity_id: str,
          actor: AppUser | str = "", **detail: Any) -> None:
    who = actor.username if isinstance(actor, AppUser) else actor
    s.add(db.AuditLog(actor=who, action=action, entity=entity,
                      entity_id=entity_id, detail=detail))


# ------------------------------------------------------------------- who is it

def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header[:7].lower() == "bearer " else None


def current_user(request: Request, s: Session = Depends(get_session)) -> AppUser:
    """A person with a session cookie, or a machine with a key. An agent key
    is not either: it may only talk to its own agent."""
    raw = _bearer(request)
    if raw is not None:
        token = auth.token_principal(s, raw)
        if token is None:
            raise HTTPException(401, "that key isn't one of ours")
        if token.role == "agent":
            raise HTTPException(403, "an agent key may only talk to its own agent")
        stand_in = AppUser(username=f"key:{token.name}", display_name=token.name,
                           role=token.role)
        stand_in.key_sites = list(token.sites or [])     # read by sites.allowed
        return stand_in

    cookie = request.cookies.get(auth.COOKIE)
    user = auth.user_for_token(s, cookie) if cookie else None
    if user is None:
        raise HTTPException(401, "sign in first")
    return user


def agent_token(agent_id: str, request: Request,
                s: Session = Depends(get_session)) -> db.ApiToken:
    raw = _bearer(request)
    token = auth.token_principal(s, raw) if raw else None
    if token is None or token.role != "agent":
        raise HTTPException(401, "that isn't an agent key")
    if token.agent_id != agent_id:
        raise HTTPException(403, f"that key belongs to {token.agent_id!r}, not {agent_id!r}")
    return token


def admin(user: AppUser = Depends(current_user)) -> AppUser:
    if user.role != "admin":
        raise HTTPException(403, "that needs an administrator")
    return user


def manager(user: AppUser = Depends(current_user)) -> AppUser:
    """A manager or an administrator. Detours and floor plans change what
    everyone at a site sees, so an operator can't."""
    if user.role not in ("admin", "manager"):
        raise HTTPException(403, "that needs a manager or an administrator")
    return user


def _may_use(s: Session, user: AppUser, printer: db.Printer) -> None:
    try:
        sites.check(s, user, printer)
    except sites.NotYours as exc:
        raise HTTPException(403, str(exc)) from None


app.mount("/studio/static", StaticFiles(directory=WEB), name="static")


class Credentials(BaseModel):
    username: str
    password: str


def _me(user: AppUser) -> dict[str, Any]:
    return {"username": user.username, "name": user.display_name, "role": user.role}


@app.post("/auth/login")
def sign_in(body: Credentials, request: Request, response: Response,
            s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        user = auth.login(s, body.username, body.password)
    except auth.LockedOut as exc:
        raise HTTPException(429, str(exc)) from None
    except auth.BadLogin as exc:
        raise HTTPException(401, str(exc)) from None

    secure = (settings.secure_cookies if settings.secure_cookies is not None
              else request.url.scheme == "https")
    response.set_cookie(
        auth.COOKIE, auth.start_session(s, user), httponly=True, samesite="lax",
        secure=secure, max_age=int(auth.SESSION_CAP.total_seconds()), path="/",
    )
    audit(s, "login", "user", user.username, actor=user)
    s.commit()
    return _me(user)


@app.post("/auth/logout")
def sign_out(request: Request, response: Response,
             s: Session = Depends(get_session)) -> dict[str, str]:
    token = request.cookies.get(auth.COOKIE)
    if token:
        auth.end_session(s, token)
    response.delete_cookie(auth.COOKIE, path="/")
    return {"signed_out": "yes"}


@app.get("/auth/me")
def whoami(user: AppUser = Depends(current_user),
           s: Session = Depends(get_session)) -> dict[str, Any]:
    mine = sites.allowed(s, user)
    return {**_me(user), "sites": None if mine is sites.ALL else sorted(mine)}


def _visible_printers(user: AppUser) -> set[str] | None:
    with SessionLocal() as s:
        where = sites.printer_filter(s, user)
        if where is None:
            return None
        return set(s.scalars(select(db.Printer.id).where(where)))


@app.get("/events")
def live_events(user: AppUser = Depends(current_user)) -> StreamingResponse:
    """Server-sent events: runs moving and printers changing health, for the
    printers this person may see. A nudge to re-read, not a record."""
    return StreamingResponse(
        events.stream(lambda: _visible_printers(user)),
        media_type="text/event-stream",
        # a proxy that buffers would hold every event until the stream ends
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


SCREENS = {
    "print": "print.html", "login": "login.html", "people": "people.html",
    "data": "data.html", "printers": "printers.html", "templates": "templates.html",
    "editor": "editor.html", "jobs": "jobs.html", "dashboard": "dashboard.html",
    "map": "map.html", "pages": "pages.html",
}


@app.get("/studio/{screen}", include_in_schema=False)
def studio(screen: str) -> FileResponse:
    if screen not in SCREENS:
        raise HTTPException(404, f"no screen {screen!r}")
    return FileResponse(WEB / "studio" / SCREENS[screen], media_type="text/html")


# ---------------------------------------------------------------- templates

def _latest_version(row: db.Template) -> db.TemplateVersion | None:
    return row.versions[-1] if row.versions else None


def _as_template(row: db.Template) -> Template:
    latest = _latest_version(row)
    return Template(
        id=row.id, name=row.name, version=latest.version if latest else 0,
        width_mm=row.width_mm, height_mm=row.height_mm, dpi=row.dpi,
        darkness=row.darkness, folder=row.folder,
        datasource=row.datasource_id, query=row.query_id,
        elements=row.elements,
    )


def _as_page(row: db.Template) -> pages.PageTemplate:
    latest = _latest_version(row)
    return pages.PageTemplate.model_validate({
        **(row.page or {}), "id": row.id, "name": row.name, "folder": row.folder,
        "version": latest.version if latest else 0,
        "datasource": row.datasource_id, "query": row.query_id})


def _definition(row: db.Template) -> dict[str, Any]:
    """What a published version freezes: a label or a page, whole."""
    return (_as_page(row) if row.kind == "page" else _as_template(row)).model_dump(mode="json")


def _size(row: db.Template) -> list[float]:
    if row.kind == "page" and row.page:
        return list(pages.PageTemplate.model_validate({**row.page, "id": row.id,
                                                       "name": row.name}).size_mm)
    return [row.width_mm, row.height_mm]


def _template_row(s: Session, template_id: str) -> db.Template:
    row = s.get(db.Template, template_id)
    if row is None:
        raise HTTPException(404, f"no template {template_id!r}")
    return row


@app.get("/templates", dependencies=[Depends(current_user)])
def list_templates(q: str = "", folder: str | None = None,
                   s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(db.Template).order_by(db.Template.name)
    if folder is not None:
        stmt = stmt.where(db.Template.folder == folder)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(db.Template.name).like(like)
                          | func.lower(db.Template.id).like(like))
    return [
        {"id": t.id, "name": t.name, "version": v.version if (v := _latest_version(t)) else 0,
         "kind": t.kind, "size_mm": _size(t), "dpi": t.dpi, "datasource": t.datasource_id,
         "query": t.query_id, "folder": t.folder, "updated_at": t.updated_at}
        for t in s.scalars(stmt)
    ]


@app.get("/templates/folders", dependencies=[Depends(current_user)])
def list_folders(s: Session = Depends(get_session)) -> dict[str, Any]:
    """What the library groups by, and how many are in each."""
    rows = s.execute(
        select(db.Template.folder, func.count()).group_by(db.Template.folder)
    ).all()
    named = sorted(((f, n) for f, n in rows if f), key=lambda r: r[0].lower())
    return {
        "total": sum(n for _, n in rows),
        "unfiled": sum(n for f, n in rows if not f),
        "folders": [{"name": f, "count": n} for f, n in named],
    }


class ImportZpl(BaseModel):
    id: str
    name: str
    zpl: str
    dpi: Literal[203, 300, 600] = 203


@app.post("/templates/import", status_code=201)
def import_zpl(body: ImportZpl, s: Session = Depends(get_session),
                 user: AppUser = Depends(admin)) -> dict[str, Any]:
    """Read a label somebody else wrote. It lands as a draft, never published,
    and the warnings say what didn't survive the trip."""
    if s.get(db.Template, body.id) is not None:
        raise HTTPException(409, f"a template called {body.id!r} already exists; "
                                 "pick another name or delete that one first")
    try:
        result = zplimport.parse(body.zpl, id=body.id, name=body.name, dpi=body.dpi)
    except zplimport.NotZpl as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception as exc:
        raise HTTPException(422, f"this label could not be read: {exc}") from None

    put_template(body.id, result.template, s)
    audit(s, "import", "template", body.id, elements=len(result.template.elements),
          warnings=result.warnings, actor=user)
    s.commit()
    return {"id": body.id, "elements": len(result.template.elements),
            "warnings": result.warnings}


@app.get("/templates/{template_id}", dependencies=[Depends(current_user)])
def get_template(template_id: str, s: Session = Depends(get_session)) -> Template:
    row = _template_row(s, template_id)
    if row.kind == "page":
        raise HTTPException(409, f"{template_id!r} is a page template; read it from "
                                 f"/pages/{template_id}")
    return _as_template(row)


@app.put("/templates/{template_id}", dependencies=[Depends(admin)])
def put_template(template_id: str, template: Template,
                 s: Session = Depends(get_session)) -> Template:
    """Saves the draft. Nothing prints from a draft — see publish."""
    row = s.get(db.Template, template_id) or db.Template(id=template_id)
    if row.kind == "page":
        raise HTTPException(409, f"{template_id!r} is a page template; save it to "
                                 f"/pages/{template_id}")
    row.name = template.name
    row.width_mm, row.height_mm, row.dpi = template.width_mm, template.height_mm, template.dpi
    row.darkness = template.darkness
    row.folder = template.folder
    row.datasource_id, row.query_id = template.datasource, template.query
    row.elements = [e.model_dump(mode="json") for e in template.elements]
    s.add(row)
    s.commit()
    return _as_template(row)


@app.post("/templates/{template_id}/publish")
def publish_template(template_id: str, s: Session = Depends(get_session),
                 user: AppUser = Depends(admin)) -> dict[str, Any]:
    """Freezes the draft as the next version. Earlier versions are never touched."""
    row = _template_row(s, template_id)
    latest = _latest_version(row)
    version = db.TemplateVersion(
        template=row, version=(latest.version + 1) if latest else 1,
        definition=_definition(row),
    )
    version.definition["version"] = version.version
    s.add(version)
    audit(s, "publish", "template", row.id, version=version.version, actor=user)
    s.commit()
    return {"id": row.id, "version": version.version, "published_at": version.published_at}


@app.delete("/templates/{template_id}", status_code=204)
def delete_template(template_id: str, s: Session = Depends(get_session),
                 user: AppUser = Depends(admin)) -> Response:
    row = _template_row(s, template_id)
    used = s.scalars(
        select(db.PrintRun.id).join(db.TemplateVersion)
        .where(db.TemplateVersion.template_id == template_id)
        .order_by(db.PrintRun.created_at.desc()).limit(3)
    ).all()
    if used:
        raise HTTPException(
            409, f"{template_id!r} has been printed, most recently as "
                 f"{', '.join(used)}. Deleting it would take that history with it.")
    s.delete(row)
    audit(s, "delete", "template", template_id, actor=user)
    s.commit()
    return Response(status_code=204)


@app.get("/templates/{template_id}/versions", dependencies=[Depends(admin)])
def list_versions(template_id: str, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    row = _template_row(s, template_id)
    return [{"version": v.version, "published_at": v.published_at} for v in row.versions]


# -------------------------------------------------------------- data sources

class DataSourceBody(BaseModel):
    name: str
    label: str = ""
    kind: Literal["sql", "rest"] = "sql"
    url: str
    headers: dict[str, str] = {}   # rest only; a value of *** keeps the stored one
    pool_size: int = 5


class ParameterBody(BaseModel):
    name: str
    type: str = "text"
    default: Any = None
    ask_at_print: bool = True
    label: str = ""


class QueryBody(BaseModel):
    datasource_id: str
    name: str
    sql: str                       # or a request path, when the source is rest
    row_path: str = ""             # rest only: where the records sit in the response
    parameters: list[ParameterBody] = []


class PreviewQuery(BaseModel):
    params: dict[str, Any] = {}
    limit: int = 25


def _datasource_json(s: Session, row: db.DataSource) -> dict[str, Any]:
    queries = s.scalars(
        select(db.SavedQuery.id).where(db.SavedQuery.datasource_id == row.id)
        .order_by(db.SavedQuery.id)
    ).all()
    return {"id": row.id, "name": row.name, "label": row.label, "kind": row.kind,
            "url": datasources.masked(row.url),
            "headers": datasources.masked_headers(row.headers),
            "pool_size": row.pool_size, "queries": list(queries)}


@app.get("/datasources", dependencies=[Depends(admin)])
def list_datasources(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [_datasource_json(s, d)
            for d in s.scalars(select(db.DataSource).order_by(db.DataSource.name))]


def _datasource_row(s: Session, datasource_id: str) -> db.DataSource:
    row = s.get(db.DataSource, datasource_id)
    if row is None:
        raise HTTPException(404, f"no data source {datasource_id!r}")
    return row


@app.get("/datasources/{datasource_id}", dependencies=[Depends(admin)])
def get_datasource(datasource_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _datasource_json(s, _datasource_row(s, datasource_id))


@app.get("/datasources/{datasource_id}/reveal", dependencies=[Depends(admin)])
def reveal_datasource(datasource_id: str, s: Session = Depends(get_session)) -> dict[str, str]:
    """The connection string with its password. Kept apart from the ordinary
    read so it never rides along with a screen that only needs to show it."""
    return {"id": datasource_id, "url": _datasource_row(s, datasource_id).url}


@app.put("/datasources/{datasource_id}")
def put_datasource(datasource_id: str, body: DataSourceBody,
                   s: Session = Depends(get_session),
                 user: AppUser = Depends(admin)) -> dict[str, Any]:
    row = s.get(db.DataSource, datasource_id)
    url = datasources.unmasked(body.url, row.url if row else None)
    headers = datasources.unmasked_headers(body.headers, row.headers if row else None)
    row = row or db.DataSource(id=datasource_id)
    row.name, row.label, row.url, row.pool_size = body.name, body.label, url, body.pool_size
    row.kind, row.headers = body.kind, headers
    s.add(row)
    audit(s, "save", "datasource", row.id, actor=user)
    s.commit()
    return _datasource_json(s, row)


@app.delete("/datasources/{datasource_id}", status_code=204)
def delete_datasource(datasource_id: str, s: Session = Depends(get_session),
                 user: AppUser = Depends(admin)) -> Response:
    row = _datasource_row(s, datasource_id)
    used = s.scalars(
        select(db.SavedQuery.id).where(db.SavedQuery.datasource_id == row.id)
    ).all()
    if used:
        raise HTTPException(
            409, f"{datasource_id!r} still has saved queries on it: {', '.join(used)}. "
                 "Delete those first.")
    s.delete(row)
    audit(s, "delete", "datasource", datasource_id, actor=user)
    s.commit()
    return Response(status_code=204)


@app.post("/datasources/{datasource_id}/test", dependencies=[Depends(admin)])
def test_datasource(datasource_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    ds = datasources.datasource(s, datasource_id)
    if ds is None:
        raise HTTPException(404, f"no data source {datasource_id!r}")
    try:
        return ds.probe()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/queries", dependencies=[Depends(admin)])
def list_queries(datasource_id: str | None = None,
                 s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(db.SavedQuery).order_by(db.SavedQuery.name)
    if datasource_id is not None:
        stmt = stmt.where(db.SavedQuery.datasource_id == datasource_id)
    return [{"id": q.id, "name": q.name, "datasource_id": q.datasource_id,
             "parameters": [p.name for p in q.parameters]}
            for q in s.scalars(stmt)]


@app.put("/queries/{query_id}", dependencies=[Depends(admin)])
def put_query(query_id: str, body: QueryBody, s: Session = Depends(get_session)) -> dict[str, Any]:
    if s.get(db.DataSource, body.datasource_id) is None:
        raise HTTPException(422, f"no data source {body.datasource_id!r}")
    row = s.get(db.SavedQuery, query_id) or db.SavedQuery(id=query_id)
    row.datasource_id, row.name, row.sql = body.datasource_id, body.name, body.sql
    row.row_path = body.row_path

    # the old parameters go first and are flushed before the new ones arrive:
    # a name reused across a save would otherwise collide with itself, because
    # SQLAlchemy orders its inserts ahead of its deletes
    row.parameters.clear()
    s.flush()
    row.parameters.extend(
        db.QueryParameter(name=p.name, type=p.type, label=p.label, position=i,
                          default=None if p.default is None else str(p.default),
                          ask_at_print=p.ask_at_print)
        for i, p in enumerate(body.parameters)
    )
    s.add(row)
    s.commit()
    return {"id": row.id, "name": row.name, "parameters": [p.name for p in row.parameters]}


@app.get("/queries/{query_id}")
def get_query(query_id: str, s: Session = Depends(get_session),
              user: AppUser = Depends(current_user)) -> dict[str, Any]:
    """An operator gets the parameters, because they have to answer them. The
    SQL and the connection behind it are not their business."""
    row = s.get(db.SavedQuery, query_id)
    if row is None:
        raise HTTPException(404, f"no saved query {query_id!r}")
    body: dict[str, Any] = {
        "id": row.id, "name": row.name,
        "parameters": [{"name": p.name, "type": p.type, "default": p.default,
                        "ask_at_print": p.ask_at_print, "label": p.label}
                       for p in row.parameters],
    }
    if user.role == "admin":
        body |= {"datasource_id": row.datasource_id, "sql": row.sql,
                 "row_path": row.row_path}
    return body


@app.get("/queries/{query_id}/columns", dependencies=[Depends(admin)])
def query_columns(query_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    """The bindable fields, for the editor's field list. No parameters needed."""
    try:
        return {"columns": datasources.columns(s, _query(s, query_id))}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"{query_id}: {exc}") from None


@app.delete("/queries/{query_id}", status_code=204)
def delete_query(query_id: str, s: Session = Depends(get_session),
                 user: AppUser = Depends(admin)) -> Response:
    row = s.get(db.SavedQuery, query_id)
    if row is None:
        raise HTTPException(404, f"no saved query {query_id!r}")
    used = s.scalars(select(db.Template.id).where(db.Template.query_id == query_id)).all()
    if used:
        raise HTTPException(
            409, f"{query_id!r} is bound to templates: {', '.join(used)}. "
                 "Point them somewhere else first.")
    s.delete(row)
    audit(s, "delete", "query", query_id, actor=user)
    s.commit()
    return Response(status_code=204)


def _query(s: Session, query_id: str) -> datasources.SavedQuery:
    q = datasources.saved_query(s, query_id)
    if q is None:
        raise HTTPException(404, f"no saved query {query_id!r}")
    return q


@app.post("/queries/{query_id}/preview", dependencies=[Depends(admin)])
def preview_query(query_id: str, body: PreviewQuery,
                  s: Session = Depends(get_session)) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        rows = datasources.run(s, _query(s, query_id), body.params, limit=body.limit)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"{query_id}: {exc}") from None
    return {"rows": jsonable_encoder(rows), "fields": datasources.describe(rows),
            "count": len(rows),
            "elapsed_ms": round((time.perf_counter() - started) * 1000)}


# ------------------------------------------------------------------ previews

class RenderPreview(BaseModel):
    params: dict[str, Any] = {}
    record: int = 0
    published: bool = False        # the latest published version, not the draft
    data: Literal["row", "columns"] = "row"
    # "columns": no rows, every column standing in for its own value. It is how
    # the editor draws a label before anyone has answered the query.


def _preview_template(s: Session, template_id: str, published: bool) -> Template:
    row = _template_row(s, template_id)
    if not published:
        return _as_template(row)
    version = _latest_version(row)
    if version is None:
        raise HTTPException(
            422, f"template {template_id!r} has no published version; publish it first")
    return Template.model_validate(version.definition)


@app.post("/templates/{template_id}/preview.png", dependencies=[Depends(current_user)])
def preview_png(template_id: str, body: RenderPreview,
                s: Session = Depends(get_session)) -> Response:
    if _template_row(s, template_id).kind == "page":
        return page_png(template_id, body, 1, s)
    t = _preview_template(s, template_id, body.published)
    row = _one_row(s, t, body)
    try:
        return Response(preview.render_png(t, row), media_type="image/png")
    except preview.RenderError as exc:
        raise HTTPException(422, str(exc)) from None


@app.post("/templates/{template_id}/preview.zpl", dependencies=[Depends(current_user)])
def preview_zpl(template_id: str, body: RenderPreview,
                s: Session = Depends(get_session)) -> Response:
    t = _preview_template(s, template_id, body.published)
    row = _one_row(s, t, body)
    try:
        labels, warnings = render_run(t, [row])
    except RenderError as exc:
        raise HTTPException(422, str(exc)) from None
    # a header must be latin-1, and a warning can carry anything a template
    # or a row put in it; json.dumps escapes everything else
    return Response("\n".join(labels), media_type="text/plain",
                    headers={"x-platen-warnings": json.dumps(warnings)})


def _one_row(s: Session, t: Template, body: RenderPreview) -> dict[str, Any]:
    if not t.query:
        return {}
    query = _query(s, t.query)
    try:
        if body.data == "columns":
            return {c: binding.Placeholder(c) for c in datasources.columns(s, query)}
        rows = datasources.run(s, query, body.params, limit=body.record + 1)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"{t.query}: {exc}") from None
    if not rows:
        raise HTTPException(422, "the query returned no rows")
    return rows[min(body.record, len(rows) - 1)]


# --------------------------------------------------------------------- pages

PAGE_ROWS = 5000                   # one preview reads at most this many rows


@app.get("/pages/{template_id}", dependencies=[Depends(current_user)])
def get_page(template_id: str, s: Session = Depends(get_session)) -> pages.PageTemplate:
    row = _template_row(s, template_id)
    if row.kind != "page":
        raise HTTPException(409, f"{template_id!r} is a label template; read it from "
                                 f"/templates/{template_id}")
    return _as_page(row)


@app.put("/pages/{template_id}")
def put_page(template_id: str, page: pages.PageTemplate, s: Session = Depends(get_session),
             user: AppUser = Depends(admin)) -> pages.PageTemplate:
    """Saves a page template's draft. Like a label, nothing prints from a draft."""
    row = s.get(db.Template, template_id)
    if row is not None and row.kind != "page":
        raise HTTPException(409, f"{template_id!r} is already a label template; "
                                 "give the page template another name")
    row = row or db.Template(id=template_id, kind="page", elements=[], dpi=203)
    row.kind, row.name, row.folder = "page", page.name, page.folder
    row.datasource_id, row.query_id = page.datasource, page.query
    row.width_mm, row.height_mm = page.size_mm
    row.page = page.model_dump(mode="json", exclude={"id", "name", "version", "folder",
                                                     "datasource", "query"})
    s.add(row)
    s.commit()
    return _as_page(row)


def _preview_page(s: Session, template_id: str, published: bool) -> pages.PageTemplate:
    row = _template_row(s, template_id)
    if row.kind != "page":
        raise HTTPException(409, f"{template_id!r} is a label template")
    if not published:
        return _as_page(row)
    version = _latest_version(row)
    if version is None:
        raise HTTPException(
            422, f"template {template_id!r} has no published version; publish it first")
    return pages.PageTemplate.model_validate(version.definition)


def _one_document(s: Session, t: pages.PageTemplate, body: RenderPreview,
                  ) -> tuple[list[dict[str, Any]], int]:
    """The rows behind document `body.record`, and how many documents there are."""
    if not t.query:
        return [{}], 1
    query = _query(s, t.query)
    try:
        if body.data == "columns":
            # three stand-in lines, so a table looks like a table
            row = {c: binding.Placeholder(c) for c in datasources.columns(s, query)}
            return [row, row, row], 1
        rows = datasources.run(s, query, body.params, limit=PAGE_ROWS)
        docs = pages.group(t, rows)
    except HTTPException:
        raise
    except pages.RenderError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception as exc:
        raise HTTPException(422, f"{t.query}: {exc}") from None
    if not docs:
        raise HTTPException(422, "the query returned no rows")
    return docs[min(body.record, len(docs) - 1)], len(docs)


def _page_pdf(s: Session, t: pages.PageTemplate, body: RenderPreview,
              ) -> tuple[bytes, list[str], int]:
    rows, documents = _one_document(s, t, body)
    try:
        pdf, warnings = pages.render_document(t, rows)
    except pages.RenderError as exc:
        raise HTTPException(422, str(exc)) from None
    return pdf, warnings, documents


@app.post("/pages/{template_id}/preview.png", dependencies=[Depends(current_user)])
def page_png(template_id: str, body: RenderPreview, page: int = 1,
             s: Session = Depends(get_session)) -> Response:
    """One page of one document, drawn from the PDF that would be printed."""
    pdf, warnings, documents = _page_pdf(s, _preview_page(s, template_id, body.published), body)
    try:
        png = pages.to_png(pdf, page)
    except pages.RenderError as exc:
        raise HTTPException(422, str(exc)) from None
    return Response(png, media_type="image/png", headers={
        "x-platen-pages": str(pages.page_count(pdf)), "x-platen-documents": str(documents),
        "x-platen-warnings": json.dumps(warnings)})


@app.post("/pages/{template_id}/preview.pdf", dependencies=[Depends(current_user)])
def page_pdf(template_id: str, body: RenderPreview,
             s: Session = Depends(get_session)) -> Response:
    pdf, warnings, _ = _page_pdf(s, _preview_page(s, template_id, body.published), body)
    return Response(pdf, media_type="application/pdf",
                    headers={"x-platen-warnings": json.dumps(warnings)})


# ---------------------------------------------------------------- print runs

class RunSpec(BaseModel):
    template_id: str
    params: dict[str, Any] = {}
    copies: int = 1
    start_at: int = 1
    skip_rows: list[int] = []      # 1-based record numbers, as the operator sees them
    separator: bool = False        # a divider label in front of the job
    pause_between: bool = False    # stop after each label, for hand-fed stock


MAX_RECORDS = 200                  # what the check will describe back
MAX_PDF_PAGES = 50


class NewRun(RunSpec):
    printer_id: str


class Prepared(BaseModel):
    version_id: int
    version: int
    records: list[dict[str, Any]]   # every record in range, skipped ones marked
    printing: int                   # how many of them will actually print
    labels: list[str]
    warnings: list[str]
    template: Any                   # a Template, or a PageTemplate for a page run
    kind: Literal["label", "page"] = "label"
    documents: list[bytes] = []     # a page run's PDFs, one per document and copy


def _prepare_pages(s: Session, body: RunSpec, version: db.TemplateVersion) -> Prepared:
    """A page run: one document per group, each a PDF, all made before any
    of it is queued. Record numbers count documents, not rows."""
    t = pages.PageTemplate.model_validate(version.definition)
    rows = datasources.run(s, _query(s, t.query), body.params) if t.query else [{}]
    try:
        groups = pages.group(t, rows)
    except pages.RenderError as exc:
        raise HTTPException(422, str(exc)) from None
    skip = set(body.skip_rows)
    in_range = [(i, g) for i, g in enumerate(groups, start=1) if i >= body.start_at]
    printing = [(i, g) for i, g in in_range if i not in skip]
    if len(printing) > pages.MAX_DOCUMENTS:
        raise HTTPException(422, f"that is {len(printing)} documents; one run takes at most "
                                 f"{pages.MAX_DOCUMENTS}. Narrow the query and print it in two")
    documents, warnings = [], []
    for i, g in printing:
        try:
            pdf, said = pages.render_document(t, g)
        except pages.RenderError as exc:
            raise HTTPException(422, f"document {i}: {exc}") from None
        warnings += [f"document {i}: {w}" for w in said]
        documents.extend([pdf] * body.copies)
    return Prepared(
        version_id=version.id, version=version.version,
        records=[{"n": n, "summary": _summarise(g[0]) if g else "", "included": n not in skip}
                 for n, g in in_range],
        printing=len(printing), labels=[], warnings=warnings, template=t,
        kind="page", documents=documents,
    )


def _prepare(s: Session, body: RunSpec) -> Prepared:
    """Pull the rows and render every label. Shared by the dry run and the
    real one so the operator's check and the print never disagree."""
    version = _latest_version(_template_row(s, body.template_id))
    if version is None:
        raise HTTPException(
            422, f"template {body.template_id!r} has no published version; publish it first")
    if version.definition.get("kind") == "page":
        return _prepare_pages(s, body, version)
    t = Template.model_validate(version.definition)

    rows = datasources.run(s, _query(s, t.query), body.params) if t.query else [{}]
    skip = set(body.skip_rows)
    in_range = [(i, r) for i, r in enumerate(rows, start=1) if i >= body.start_at]
    printing = [(i, r) for i, r in in_range if i not in skip]

    try:
        labels, warnings = render_run(t, [r for _, r in printing], copies=body.copies)
    except RenderError as exc:
        raise HTTPException(422, str(exc)) from None
    # every record in range comes back, skipped ones marked: a list that drops
    # what you untick makes unticking a one-way door
    return Prepared(
        version_id=version.id, version=version.version,
        records=[{"n": n, "summary": _summarise(r), "included": n not in skip}
                 for n, r in in_range],
        printing=len(printing), labels=labels, warnings=warnings, template=t,
    )


def _summarise(row: dict[str, Any]) -> str:
    """Enough of a record for someone to recognise it in a list. Image columns
    are four kilobytes of nothing anyone can read, so they are left out."""
    bits = []
    for value in row.values():
        if value is None or isinstance(value, (bytes, memoryview)):
            continue
        text = str(value)
        if len(text) > 64:                            # a base64 image, or prose
            continue
        bits.append(text)
        if len(bits) == 3:
            break
    return " · ".join(bits)[:180]


def _with_separator(p: Prepared, run_id: str) -> list[str]:
    when = auth.now().strftime("%d %b %Y %H:%M")
    return [separator(p.template, run_id, len(p.labels), when), *p.labels]


@app.post("/runs/check", dependencies=[Depends(current_user)])
def check_run(body: RunSpec, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Everything a run would do short of writing it down or queueing it."""
    started = time.perf_counter()
    p = _prepare(s, body)
    if p.kind == "page":
        return {
            "kind": "page",
            "records": p.printing,
            "record_list": p.records[:MAX_RECORDS],
            "records_capped": len(p.records) > MAX_RECORDS,
            "documents": len(p.documents),
            "labels": len(p.documents),        # what the print screen counts
            "pages": sum(pages.page_count(d) for d in p.documents),
            "warnings": p.warnings,
            "template_version": p.version,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    return {
        "kind": "label",
        "records": p.printing,
        "record_list": p.records[:MAX_RECORDS],
        "records_capped": len(p.records) > MAX_RECORDS,
        "labels": len(p.labels) + (1 if body.separator else 0),
        "warnings": p.warnings,
        "template_version": p.version,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }


@app.post("/runs/preview.pdf", dependencies=[Depends(current_user)])
def run_pdf(body: RunSpec, s: Session = Depends(get_session)) -> RawResponse:
    """The run as a PDF, to look at before committing a roll of stock to it."""
    p = _prepare(s, body)
    if p.kind == "page":
        if not p.documents:
            raise HTTPException(422, "there is nothing to draw")
        name = f"{body.template_id}-{auth.now():%Y%m%d-%H%M}.pdf"
        return RawResponse(
            pages.merge(p.documents[:MAX_PDF_PAGES]), media_type="application/pdf",
            headers={"content-disposition": f'attachment; filename="{name}"',
                     "x-platen-documents": str(len(p.documents))})
    rows = (datasources.run(s, _query(s, p.template.query), body.params)
            if p.template.query else [{}])
    skip = set(body.skip_rows)
    chosen = [r for i, r in enumerate(rows, start=1)
              if i >= body.start_at and i not in skip]

    sheets: list[Image.Image] = []
    for row in chosen:
        if len(sheets) >= MAX_PDF_PAGES:
            break
        for _ in range(body.copies):
            if len(sheets) >= MAX_PDF_PAGES:
                break
            try:
                sheets.append(Image.open(io.BytesIO(preview.render_png(p.template, row))))
            except preview.RenderError as exc:
                raise HTTPException(422, str(exc)) from None
    if not sheets:
        raise HTTPException(422, "there is nothing to draw")

    out = io.BytesIO()
    sheets[0].convert("L").save(out, "PDF", save_all=True,
                               append_images=[page.convert("L") for page in sheets[1:]],
                               resolution=p.template.dpi)
    name = f"{body.template_id}-{auth.now():%Y%m%d-%H%M}.pdf"
    return RawResponse(
        out.getvalue(), media_type="application/pdf",
        headers={"content-disposition": f'attachment; filename="{name}"',
                 "x-platen-pages": str(len(sheets)),
                 "x-platen-labels": str(len(p.labels))},
    )


def _run_json(run: db.PrintRun) -> dict[str, Any]:
    return {
        "id": run.id, "status": run.status, "printed": run.printed, "total": run.total,
        "error": run.error, "attempts": run.attempts,
        "template_id": run.template_version.template_id,
        "template_name": run.template_version.definition.get("name"),
        "template_version": run.template_version.version, "printer_id": run.printer_id,
        "moving_to": run.move_to,
        "params": run.params, "copies": run.copies, "pause_between": run.pause_between,
        "warnings": [f"row {w.row_no}: {w.message}" if w.row_no else w.message
                     for w in run.warnings],
        "created_at": run.created_at, "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


@app.post("/runs", status_code=202)
def create_run(body: NewRun, s: Session = Depends(get_session),
                 user: AppUser = Depends(current_user)) -> dict[str, Any]:
    printer = s.get(db.Printer, body.printer_id)
    if printer is None:
        raise HTTPException(404, f"no printer {body.printer_id!r}")
    _may_use(s, user, printer)
    asked_for = printer
    if printer.detour_to:
        printer = s.get(db.Printer, printer.detour_to) or printer
        if not sites.may_use(s, user, printer):
            raise HTTPException(
                409, f"{asked_for.name} is detoured to {printer.name}, which isn't at one "
                     "of your sites; ask a manager to clear the detour")

    # every label renders here, before a run row exists, let alone a job
    p = _prepare(s, body)
    if printer.kind != p.kind:
        raise HTTPException(
            422, f"{printer.name} is a {printer.kind} printer and this template prints "
                 f"{p.kind}s; pick a {p.kind} printer")

    run_id = f"JOB-{uuid.uuid4().hex[:6].upper()}"
    if p.kind == "page":
        if body.separator:
            p.warnings.append("a separator is a label; a page run leaves it out")
        rows = [db.RunLabel(seq=i, zpl="", body=d) for i, d in enumerate(p.documents, start=1)]
    else:
        labels = _with_separator(p, run_id) if body.separator else p.labels
        rows = [db.RunLabel(seq=i, zpl=z) for i, z in enumerate(labels, start=1)]
    run = db.PrintRun(
        id=run_id, template_version_id=p.version_id,
        printer_id=printer.id, params=body.params, copies=body.copies,
        pause_between=body.pause_between, total=len(rows),
        labels=rows,
        warnings=[_warning(w) for w in p.warnings],
    )
    s.add(run)
    audit(s, "create", "run", run.id, template_id=body.template_id, template_version=p.version,
          printer_id=printer.id, asked_for=asked_for.id, labels=len(rows), kind=p.kind,
          skip_rows=body.skip_rows, actor=user)
    s.commit()

    try:
        jobs.enqueue(run.id)
    except Exception as exc:
        run.status, run.error = "failed", f"could not queue: {type(exc).__name__}: {exc}"
        s.commit()
        events.run_changed(run)
        raise HTTPException(503, run.error) from None
    events.run_changed(run)
    return {"id": run.id, "labels": run.total, "warnings": p.warnings,
            "template_version": p.version, "printer_id": printer.id,
            "detoured_from": asked_for.id if asked_for is not printer else None}


def _warning(text: str) -> db.RunWarning:
    """render_run prefixes warnings with "row N: "; the table keeps N as a column."""
    head, _, rest = text.partition(": ")
    if head.startswith("row ") and head[4:].isdigit():
        return db.RunWarning(row_no=int(head[4:]), message=rest)
    return db.RunWarning(row_no=None, message=text)


def _run_row(s: Session, run_id: str, user: AppUser) -> db.PrintRun:
    run = s.get(db.PrintRun, run_id)
    if run is None:
        raise HTTPException(404, f"no run {run_id!r}")
    printer = s.get(db.Printer, run.printer_id)
    if printer is not None:
        _may_use(s, user, printer)
    return run


def _scope_runs(stmt: Any, s: Session, user: AppUser) -> Any:
    """Narrow a select over PrintRun to the printers this person may see."""
    where = sites.printer_filter(s, user)
    if where is None:
        return stmt
    return stmt.join(db.Printer, db.Printer.id == db.PrintRun.printer_id).where(where)


@app.get("/runs")
def list_runs(s: Session = Depends(get_session), limit: int = 50,
              status: str | None = None,
              user: AppUser = Depends(current_user)) -> list[dict[str, Any]]:
    stmt = (
        select(db.PrintRun)
        # a dashboard that asks once per row falls over on a busy morning
        .options(selectinload(db.PrintRun.template_version),
                 selectinload(db.PrintRun.warnings))
        .order_by(db.PrintRun.created_at.desc(), db.PrintRun.id.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(db.PrintRun.status.in_(status.split(",")))
    return [_run_json(r) for r in s.scalars(_scope_runs(stmt, s, user))]


@app.get("/runs/{run_id}")
def get_run(run_id: str, s: Session = Depends(get_session),
            user: AppUser = Depends(current_user)) -> dict[str, Any]:
    return _run_json(_run_row(s, run_id, user))


@app.post("/runs/{run_id}/retry", status_code=202)
def retry_run(run_id: str, s: Session = Depends(get_session),
                 user: AppUser = Depends(current_user)) -> dict[str, Any]:
    """Put a stopped run back on the queue. It resumes: labels that already
    came out of the printer are not sent again."""
    run = _run_row(s, run_id, user)
    if run.status in ("done", "printing", "queued"):
        raise HTTPException(409, f"{run_id} is {run.status}; there is nothing to retry")
    left = jobs.remaining(s, run_id)
    if not left:
        raise HTTPException(409, f"every label in {run_id} has already printed")

    run.status, run.cancel_requested, run.error, run.finished_at = "queued", False, None, None
    audit(s, "retry", "run", run_id, remaining=left, actor=user)
    s.commit()
    jobs.enqueue(run_id)
    events.run_changed(run)
    return {"id": run_id, "status": "queued", "remaining": left}


@app.post("/runs/{run_id}/continue", status_code=202)
def continue_run(run_id: str, s: Session = Depends(get_session),
                 user: AppUser = Depends(current_user)) -> dict[str, Any]:
    """The next label of a hand-fed job."""
    run = _run_row(s, run_id, user)
    if run.status != "waiting":
        raise HTTPException(409, f"{run_id} is {run.status}; it is not waiting on anyone")
    run.status = "queued"
    audit(s, "continue", "run", run_id, actor=user)
    s.commit()
    jobs.enqueue(run_id)
    events.run_changed(run)
    return {"id": run_id, "status": "queued", "remaining": jobs.remaining(s, run_id)}


@app.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(run_id: str, s: Session = Depends(get_session),
                 user: AppUser = Depends(current_user)) -> dict[str, str]:
    run = _run_row(s, run_id, user)
    if run.status in ("done", "failed", "cancelled"):
        return {"id": run_id, "status": run.status}
    if run.status == "blocked":
        # nothing is running it to notice a flag, so it stops here
        run.status, run.finished_at = "cancelled", auth.now()
        audit(s, "cancel", "run", run_id, actor=user)
        s.commit()
        events.run_changed(run)
        return {"id": run_id, "status": "cancelled"}
    audit(s, "cancel", "run", run_id, actor=user)
    jobs.cancel(s, run_id)
    return {"id": run_id, "status": "cancelling"}


class MoveBody(BaseModel):
    printer_id: str
    confirm: bool = False          # a hand-fed run only moves when someone says so


MOVABLE = ("queued", "printing", "retrying", "paused", "waiting", "failed", "blocked")


def _move(s: Session, run: db.PrintRun, target: db.Printer, user: AppUser, *,
          confirm: bool = False) -> dict[str, Any]:
    """Point what is left of a run at another printer. Labels already out stay
    out: the worker only ever sends labels with no printed_at."""
    if run.status not in MOVABLE:
        raise HTTPException(409, f"{run.id} is {run.status}; there is nothing left to move")
    if target.id == run.printer_id and run.move_to:
        # still printing here, on its way somewhere else: moving it "back" is
        # taking the move back, which is what an undo of it means
        left = jobs.remaining(s, run.id)
        gone_to, run.move_to = run.move_to, None
        audit(s, "move", "run", run.id, actor=user, **{"from": gone_to, "to": target.id},
              remaining=left, called_off=True)
        s.commit()
        events.run_changed(run)
        return {"id": run.id, "from": gone_to, "to": target.id, "status": run.status,
                "remaining": left, "moving": False}
    if target.id == run.printer_id:
        raise HTTPException(409, f"{run.id} is already on {target.name}")
    _may_use(s, user, target)
    if target.detour_to:
        raise HTTPException(409, f"{target.name} is detoured to {target.detour_to}; "
                                 "move the run there instead")
    run_kind = run.template_version.definition.get("kind", "label")
    if target.kind != run_kind:
        raise HTTPException(
            422, f"{run.id} prints {run_kind}s and {target.name} is a {target.kind} printer")
    drawn_at = run.template_version.definition.get("dpi", 203)
    if run_kind == "label" and target.dpi != drawn_at:
        # the labels are already ZPL at the template's dot pitch; on another
        # pitch every one comes out the wrong size
        raise HTTPException(
            422, f"{run.id}'s labels were drawn for {drawn_at} dpi and {target.name} prints at "
                 f"{target.dpi}. Start a new run on {target.name} instead.")
    left = jobs.remaining(s, run.id)
    if not left:
        raise HTTPException(409, f"every label in {run.id} has already printed")
    source = s.get(db.Printer, run.printer_id)
    if run.status == "waiting" and not confirm:
        raise HTTPException(
            409, f"{run.id} is waiting on someone feeding stock at "
                 f"{source.name if source else run.printer_id}. Moving it means the next "
                 f"label comes out at {target.name}; confirm to move it anyway.")

    seen, from_id = run.status, run.printer_id
    if seen == "printing":
        run.move_to = target.id               # the worker moves it between labels
    else:
        # only if nothing picked it up since it was read: a worker that did is
        # now "printing", and is asked through move_to instead
        changes: dict[str, Any] = {"printer_id": target.id}
        # a failed run, or one sitting out a retry on a printer that isn't
        # answering, goes now; the worker's claim makes a late retry harmless
        if seen in ("failed", "retrying", "blocked"):
            changes.update(status="queued", error=None, finished_at=None,
                           cancel_requested=False)
        done = s.execute(update(db.PrintRun)
                         .where(db.PrintRun.id == run.id, db.PrintRun.status == seen)
                         .values(**changes)).rowcount
        if not done:
            s.rollback()
            s.refresh(run)
            if run.status != "printing":
                raise HTTPException(409, f"{run.id} changed while it was being moved; try again")
            run.move_to = target.id
    audit(s, "move", "run", run.id, actor=user, **{"from": from_id, "to": target.id},
          remaining=left)
    s.commit()
    s.refresh(run)
    if seen in ("failed", "retrying", "blocked") and run.status == "queued":
        jobs.enqueue(run.id)
    events.run_changed(run)
    return {"id": run.id, "from": from_id, "to": target.id, "status": run.status,
            "remaining": left, "moving": run.move_to is not None}


@app.post("/runs/{run_id}/move")
def move_run(run_id: str, body: MoveBody, s: Session = Depends(get_session),
             user: AppUser = Depends(current_user)) -> dict[str, Any]:
    """Undo is the same call with the printer it came from."""
    run = _run_row(s, run_id, user)
    return _move(s, run, _printer_row(s, body.printer_id), user, confirm=body.confirm)


# ------------------------------------------------------------------ printers

class PrinterBody(BaseModel):
    name: str
    model: str = ""
    dpi: int = 203
    kind: Literal["label", "page"] = "label"
    transport: dict[str, Any]
    site_id: str | None = None
    grid_x: int | None = None
    grid_y: int | None = None


def _printer_json(row: db.Printer, online: bool | None) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "model": row.model, "dpi": row.dpi,
            "transport": {"kind": row.transport_kind, **row.transport_config},
            "site_id": row.site_id, "grid": [row.grid_x, row.grid_y],
            "detour_to": row.detour_to, "kind": row.kind,
            "health": {"state": row.health, "problems": row.health_problems or [],
                       "checked_at": row.health_at},
            "online": online}


def _probe(row: db.Printer, agents: dict[str, bool]) -> bool | None:
    if row.transport_kind == "agent":
        return agents.get((row.transport_config or {}).get("agent_id"), False)
    try:
        return printers.from_row(row).transport.probe()
    except Exception:
        return False


def _printers_for(s: Session, user: AppUser) -> list[db.Printer]:
    stmt = select(db.Printer).order_by(db.Printer.name)
    where = sites.printer_filter(s, user)
    return list(s.scalars(stmt if where is None else stmt.where(where)))


@app.get("/printers")
def list_printers(probe: bool = True, s: Session = Depends(get_session),
                  user: AppUser = Depends(current_user)) -> list[dict[str, Any]]:
    """Probing talks to every printer, and an offline one only answers when it
    times out — so they are asked all at once rather than one after another."""
    rows = _printers_for(s, user)
    if not probe:
        return [_printer_json(r, None) for r in rows]
    # agents are a database read, so they are answered here rather than in a
    # thread that would need a session of its own
    agents = {a.id: _agent_online(a) for a in s.scalars(select(db.PrintAgent))}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(rows)))) as pool:
        states = list(pool.map(lambda r: _probe(r, agents), rows))
    return [_printer_json(r, online) for r, online in zip(rows, states, strict=True)]


def _printer_row(s: Session, printer_id: str) -> db.Printer:
    row = s.get(db.Printer, printer_id)
    if row is None:
        raise HTTPException(404, f"no printer {printer_id!r}")
    return row


@app.get("/printers/{printer_id}", dependencies=[Depends(admin)])
def get_printer(printer_id: str, probe: bool = False,
                s: Session = Depends(get_session)) -> dict[str, Any]:
    row = _printer_row(s, printer_id)
    if not probe:
        return _printer_json(row, None)
    agents = {a.id: _agent_online(a) for a in s.scalars(select(db.PrintAgent))}
    return _printer_json(row, _probe(row, agents))


@app.delete("/printers/{printer_id}", status_code=204)
def delete_printer(printer_id: str, s: Session = Depends(get_session),
                 user: AppUser = Depends(admin)) -> Response:
    row = _printer_row(s, printer_id)
    used = s.scalars(
        select(db.PrintRun.id).where(db.PrintRun.printer_id == printer_id)
        .order_by(db.PrintRun.created_at.desc()).limit(3)
    ).all()
    if used:
        raise HTTPException(
            409, f"{printer_id!r} has print runs against it, most recently "
                 f"{', '.join(used)}. Deleting it would take their history with it.")
    s.delete(row)
    audit(s, "delete", "printer", printer_id, actor=user)
    s.commit()
    return Response(status_code=204)


@app.put("/printers/{printer_id}")
def put_printer(printer_id: str, body: PrinterBody,
                s: Session = Depends(get_session),
                 user: AppUser = Depends(admin)) -> dict[str, Any]:
    config = dict(body.transport)
    kind = config.pop("kind", None)
    if kind is None:
        raise HTTPException(422, "transport.kind is required: tcp, cups or agent")
    if kind == "cups":
        # CUPS turns a PDF into what an office printer speaks; ZPL goes through
        # untouched, which is the default and so isn't written down
        config.pop("raw", None)
        if body.kind == "page":
            config["raw"] = False
    try:
        printers.build(kind, config)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, f"transport: {exc}") from None
    site = s.get(db.Site, body.site_id) if body.site_id is not None else None
    if body.site_id is not None and site is None:
        raise HTTPException(422, f"no site {body.site_id!r}; make the site first")
    if site is not None and site.archived_at is not None:
        raise HTTPException(422, f"{site.name} is archived; restore it first")
    row = s.get(db.Printer, printer_id) or db.Printer(id=printer_id)
    row.name, row.model, row.dpi = body.name, body.model, body.dpi
    row.site_id, row.grid_x, row.grid_y = body.site_id, body.grid_x, body.grid_y
    row.kind = body.kind
    row.transport_kind, row.transport_config = kind, config
    s.add(row)
    audit(s, "save", "printer", row.id, transport=kind, site_id=row.site_id, actor=user)
    s.commit()
    return _printer_json(row, None)


class DetourBody(BaseModel):
    to: str | None                 # None clears it


@app.post("/printers/{printer_id}/detour")
def detour_printer(printer_id: str, body: DetourBody, s: Session = Depends(get_session),
                   user: AppUser = Depends(manager)) -> dict[str, Any]:
    """Send a printer's work somewhere else until somebody says stop. Its
    unfinished runs move now, except a hand-fed one, which someone is standing
    at; new runs follow the detour when they are made."""
    row = _printer_row(s, printer_id)
    _may_use(s, user, row)
    if body.to is None:
        row.detour_to = None
        audit(s, "detour", "printer", printer_id, actor=user, to=None)
        s.commit()
        events.publish("printer", printer_id=row.id, site_id=row.site_id, detour_to=None)
        return {"id": row.id, "detour_to": None, "moved": [], "left": []}

    target = _printer_row(s, body.to)
    _may_use(s, user, target)
    if target.id == row.id:
        raise HTTPException(422, "a printer can't be detoured to itself")
    if target.detour_to:
        raise HTTPException(409, f"{target.name} is itself detoured to {target.detour_to}; "
                                 "detour to that one, or clear it first")
    into = s.scalars(select(db.Printer.name).where(db.Printer.detour_to == row.id)).all()
    if into:
        raise HTTPException(409, f"{', '.join(into)} already detour(s) to {row.name}; "
                                 "point them somewhere else first")
    if target.kind != row.kind:
        raise HTTPException(422, f"{row.name} is a {row.kind} printer and {target.name} a "
                                 f"{target.kind} printer; a detour stays with its own kind")
    if row.kind == "label" and target.dpi != row.dpi:
        raise HTTPException(422, f"{row.name} prints at {row.dpi} dpi and {target.name} at "
                                 f"{target.dpi}; its labels would come out the wrong size")
    row.detour_to = target.id
    audit(s, "detour", "printer", printer_id, actor=user, to=target.id)
    s.commit()

    moved, left = [], []
    unfinished = s.scalars(select(db.PrintRun).where(
        db.PrintRun.printer_id == row.id, db.PrintRun.status.in_(MOVABLE))).all()
    for run in unfinished:
        if run.status == "waiting":
            left.append({"id": run.id, "why": "waiting on someone feeding stock"})
            continue
        try:
            _move(s, run, target, user)
            moved.append(run.id)
        except HTTPException as exc:
            left.append({"id": run.id, "why": exc.detail})
    events.publish("printer", printer_id=row.id, site_id=row.site_id, detour_to=target.id)
    return {"id": row.id, "detour_to": target.id, "moved": moved, "left": left}


class PositionBody(BaseModel):
    x: int | None
    y: int | None


@app.put("/printers/{printer_id}/position")
def place_printer(printer_id: str, body: PositionBody, s: Session = Depends(get_session),
                  user: AppUser = Depends(manager)) -> dict[str, Any]:
    """Where a printer stands on its site's floor plan."""
    row = _printer_row(s, printer_id)
    _may_use(s, user, row)
    row.grid_x, row.grid_y = body.x, body.y
    audit(s, "place", "printer", printer_id, actor=user, x=body.x, y=body.y)
    s.commit()
    events.publish("printer", printer_id=row.id, site_id=row.site_id, grid=[body.x, body.y])
    return _printer_json(row, None)


# ----------------------------------------------------------------- saved runs

class SavedRunBody(BaseModel):
    name: str
    template_id: str
    printer_id: str | None = None
    params: dict[str, Any] = {}
    copies: int = 1
    separator: bool = False
    pause_between: bool = False


def _saved_run_json(row: db.SavedRun) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "template_id": row.template_id,
            "printer_id": row.printer_id, "params": row.params, "copies": row.copies,
            "separator": row.separator, "pause_between": row.pause_between,
            "created_by": row.created_by, "updated_at": row.updated_at}


@app.get("/saved-runs")
def list_saved_runs(s: Session = Depends(get_session),
                    user: AppUser = Depends(current_user)) -> list[dict[str, Any]]:
    """A saved run pinned to a printer at somebody else's site is theirs, not
    yours; one with no printer is anyone's."""
    usable = {p.id for p in _printers_for(s, user)}
    return [_saved_run_json(r) for r in s.scalars(
        select(db.SavedRun).order_by(db.SavedRun.name))
        if r.printer_id is None or r.printer_id in usable]


@app.put("/saved-runs/{saved_id}")
def put_saved_run(saved_id: str, body: SavedRunBody, s: Session = Depends(get_session),
                  user: AppUser = Depends(current_user)) -> dict[str, Any]:
    if s.get(db.Template, body.template_id) is None:
        raise HTTPException(422, f"no template {body.template_id!r} to save a run for")
    row = s.get(db.SavedRun, saved_id) or db.SavedRun(id=saved_id, created_by=user.username)
    row.name, row.template_id, row.printer_id = body.name, body.template_id, body.printer_id
    row.params, row.copies = body.params, body.copies
    row.separator, row.pause_between = body.separator, body.pause_between
    s.add(row)
    s.commit()
    return _saved_run_json(row)


@app.delete("/saved-runs/{saved_id}", status_code=204)
def delete_saved_run(saved_id: str, s: Session = Depends(get_session),
                     user: AppUser = Depends(current_user)) -> Response:
    row = s.get(db.SavedRun, saved_id)
    if row is None:
        raise HTTPException(404, f"no saved run {saved_id!r}")
    s.delete(row)
    audit(s, "delete", "saved_run", saved_id, actor=user)
    s.commit()
    return Response(status_code=204)


# ------------------------------------------------------------- looking after it

class RetentionBody(BaseModel):
    labels_days: int | None = None
    runs_days: int | None = None
    audit_days: int | None = None
    agent_jobs_days: int | None = None


@app.get("/maintenance/retention", dependencies=[Depends(admin)])
def get_retention(s: Session = Depends(get_session)) -> dict[str, Any]:
    return retention.settings(s)


@app.put("/maintenance/retention")
def put_retention(body: RetentionBody, s: Session = Depends(get_session),
                  user: AppUser = Depends(admin)) -> dict[str, Any]:
    """Days to keep each thing. Zero keeps it, which somebody has to choose."""
    kept = retention.set_settings(s, body.model_dump(exclude_none=True))
    audit(s, "retention", "maintenance", "settings", actor=user, **kept)
    s.commit()
    return kept


@app.post("/maintenance/prune")
def run_prune(s: Session = Depends(get_session),
              user: AppUser = Depends(admin)) -> dict[str, Any]:
    removed = retention.prune(s)
    audit(s, "prune", "maintenance", "all", actor=user, **removed)
    s.commit()
    return {"removed": removed, "ran_at": retention.now()}


# ------------------------------------------------------------------ the front

ACTIVE = ("queued", "printing", "retrying")


@app.get("/dashboard")
def dashboard(s: Session = Depends(get_session),
              user: AppUser = Depends(current_user)) -> dict[str, Any]:
    """The few numbers worth seeing on the way past, and what is on the queue —
    for the sites this person looks after."""
    since = auth.now().replace(hour=0, minute=0, second=0, microsecond=0)

    printed_today = s.scalar(_scope_runs(
        select(func.count()).select_from(db.RunLabel)
        .join(db.PrintRun, db.PrintRun.id == db.RunLabel.run_id)
        .where(db.RunLabel.printed_at >= since), s, user)) or 0
    queued = s.scalar(_scope_runs(
        select(func.count()).select_from(db.PrintRun)
        .where(db.PrintRun.status.in_(ACTIVE)), s, user)) or 0
    failed_today = s.scalar(_scope_runs(
        select(func.count()).select_from(db.PrintRun)
        .where(db.PrintRun.status == "failed", db.PrintRun.created_at >= since),
        s, user)) or 0

    printer_rows = _printers_for(s, user)
    agents = {a.id: _agent_online(a) for a in s.scalars(select(db.PrintAgent))}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(printer_rows)))) as pool:
        states = list(pool.map(lambda r: _probe(r, agents), printer_rows))

    runs = list(s.scalars(_scope_runs(
        select(db.PrintRun)
        .options(selectinload(db.PrintRun.template_version),
                 selectinload(db.PrintRun.warnings))
        .order_by(db.PrintRun.created_at.desc()).limit(8), s, user)))

    templates = list(s.scalars(
        select(db.Template).order_by(db.Template.updated_at.desc()).limit(5)))

    return {
        "printed_today": printed_today,
        "queued": queued,
        "failed_today": failed_today,
        "paused": jobs.paused(s),
        "printers": {"total": len(printer_rows), "online": sum(1 for x in states if x)},
        "runs": [_run_json(r) for r in runs],
        "templates": [
            {"id": t.id, "name": t.name, "dpi": t.dpi, "size_mm": [t.width_mm, t.height_mm],
             "version": t.versions[-1].version if t.versions else 0,
             "updated_at": t.updated_at}
            for t in templates
        ],
        "datasources": [{"id": d.id, "name": d.name, "kind": d.kind}
                        for d in s.scalars(select(db.DataSource).order_by(db.DataSource.name))],
    }


@app.post("/queue/pause")
def pause_queue(s: Session = Depends(get_session),
                user: AppUser = Depends(admin)) -> dict[str, Any]:
    """Hold everything. A run already printing stops between labels."""
    jobs.set_paused(s, True)
    audit(s, "pause", "queue", "all", actor=user)
    s.commit()
    events.publish("queue", paused=True)
    return {"paused": True}


@app.post("/queue/resume")
def resume_queue(s: Session = Depends(get_session),
                 user: AppUser = Depends(admin)) -> dict[str, Any]:
    """Let it go again, and put every held run back on the queue. They resume
    where they stopped, the same as a retry."""
    jobs.set_paused(s, False)
    held = list(s.scalars(select(db.PrintRun).where(db.PrintRun.status == "paused")))
    for run in held:
        run.status = "queued"
    audit(s, "resume", "queue", "all", actor=user, resumed=len(held))
    s.commit()
    for run in held:
        jobs.enqueue(run.id)
        events.run_changed(run)
    events.publish("queue", paused=False)
    return {"paused": False, "resumed": len(held)}


# ---------------------------------------------------------------------- sites

class SiteBody(BaseModel):
    name: str
    map_x: int = 0
    map_y: int = 0


def _site_json(s: Session, row: db.Site) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "map": [row.map_x, row.map_y],
            "archived_at": row.archived_at,
            "printers": s.scalar(select(func.count()).select_from(db.Printer)
                                 .where(db.Printer.site_id == row.id)) or 0}


@app.get("/sites")
def list_sites(archived: bool = False, s: Session = Depends(get_session),
               user: AppUser = Depends(current_user)) -> list[dict[str, Any]]:
    """The sites this person looks after; every site, for an administrator.
    Archived ones only when asked for."""
    mine = sites.allowed(s, user)
    rows = s.scalars(select(db.Site).order_by(db.Site.name))
    return [_site_json(s, r) for r in rows
            if (mine is sites.ALL or r.id in mine)
            and (archived or r.archived_at is None)]


@app.put("/sites/{site_id}")
def put_site(site_id: str, body: SiteBody, s: Session = Depends(get_session),
             user: AppUser = Depends(admin)) -> dict[str, Any]:
    if not body.name.strip():
        raise HTTPException(422, "a site needs a name")
    row = s.get(db.Site, site_id) or db.Site(id=site_id)
    if row.archived_at is not None:
        raise HTTPException(409, f"{row.name} is archived; restore it before changing it")
    row.name, row.map_x, row.map_y = body.name.strip(), body.map_x, body.map_y
    s.add(row)
    audit(s, "save", "site", site_id, actor=user)
    s.commit()
    return _site_json(s, row)


def _site_row(s: Session, site_id: str) -> db.Site:
    row = s.get(db.Site, site_id)
    if row is None:
        raise HTTPException(404, f"no site {site_id!r}")
    return row


@app.post("/sites/{site_id}/archive")
def archive_site(site_id: str, s: Session = Depends(get_session),
                 user: AppUser = Depends(admin)) -> dict[str, Any]:
    """A site is archived, never deleted. Refused while printers are in it:
    nobody could reach them, and moving them out is a choice somebody should
    make on purpose. Who looked after it is kept for a restore."""
    row = _site_row(s, site_id)
    inside = s.scalars(select(db.Printer.name).where(db.Printer.site_id == site_id)
                       .order_by(db.Printer.name)).all()
    if inside:
        raise HTTPException(
            409, f"{row.name} still has {len(inside)} printer(s) in it: "
                 f"{', '.join(inside[:5])}. Move them to another site first.")
    if row.archived_at is None:
        row.archived_at = auth.now()
        audit(s, "archive", "site", site_id, actor=user)
        s.commit()
    return _site_json(s, row)


@app.post("/sites/{site_id}/restore")
def restore_site(site_id: str, s: Session = Depends(get_session),
                 user: AppUser = Depends(admin)) -> dict[str, Any]:
    row = _site_row(s, site_id)
    if row.archived_at is not None:
        row.archived_at = None
        audit(s, "restore", "site", site_id, actor=user)
        s.commit()
    return _site_json(s, row)


# ------------------------------------------------------------ people and keys

class UserBody(BaseModel):
    name: str = ""
    role: Literal["admin", "manager", "operator"] = "operator"
    disabled: bool = False
    password: str | None = None    # required when there is no such user yet
    sites: list[str] | None = None  # None leaves them as they are


class PasswordBody(BaseModel):
    password: str


class KeyBody(BaseModel):
    name: str
    role: Literal["admin", "operator"] = "operator"
    sites: list[str] = []          # an operator key's sites; none means every site


def _user_json(s: Session, row: AppUser) -> dict[str, Any]:
    return {"username": row.username, "name": row.display_name, "role": row.role,
            "sites": sites.assigned(s, row.username),
            "disabled": row.disabled, "locked": bool(row.locked_until
                                                     and row.locked_until > auth.now()),
            "created_at": row.created_at, "last_login_at": row.last_login_at}


def _user_row(s: Session, username: str) -> AppUser:
    row = s.get(AppUser, username)
    if row is None:
        raise HTTPException(404, f"no user {username!r}")
    return row


def _other_admins(s: Session, username: str) -> int:
    return s.scalar(
        select(func.count()).select_from(AppUser)
        .where(AppUser.role == "admin", AppUser.disabled.is_(False),
               AppUser.username != username)
    ) or 0


@app.get("/users", dependencies=[Depends(admin)])
def list_users(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [_user_json(s, u) for u in s.scalars(select(AppUser).order_by(AppUser.username))]


@app.put("/users/{username}")
def put_user(username: str, body: UserBody, s: Session = Depends(get_session),
             user: AppUser = Depends(admin)) -> dict[str, Any]:
    username = username.strip().lower()
    row = s.get(AppUser, username)

    # the one thing an administrator must not be able to do is shut the door
    # on themselves and have nobody left holding a key
    losing_rights = body.role != "admin" or body.disabled
    if row is not None and username == user.username and losing_rights:
        raise HTTPException(
            409, "you can't take your own administrator rights away; "
                 "ask another administrator to do it")
    if (row is not None and row.role == "admin" and losing_rights
            and not _other_admins(s, username)):
        raise HTTPException(409, f"{username} is the last administrator")

    # checked before anything is written, so a bad site list never leaves
    # half a user behind
    if body.role == "operator" and body.sites and len(set(body.sites)) > 1:
        raise HTTPException(422, "an operator works at one site; make them a "
                                 "manager to look after more than one")
    missing = [i for i in body.sites or [] if s.get(db.Site, i) is None]
    if missing:
        raise HTTPException(422, f"no site {', '.join(repr(m) for m in missing)}")
    kept = sites.assigned(s, username) if row is not None else []
    archived = [i for i in body.sites or [] if i not in kept
                and s.get(db.Site, i).archived_at is not None]
    if archived:
        raise HTTPException(422, f"{', '.join(archived)} is archived; restore it first")
    if (body.sites is None and body.role == "operator" and len(kept) > 1):
        raise HTTPException(422, f"{username} looks after {len(kept)} sites; say "
                                 "which one they keep before making them an operator")

    if row is None:
        if not body.password:
            raise HTTPException(422, "a new user needs a password")
        try:
            row = auth.create_user(s, username, body.password, role=body.role,
                                   display_name=body.name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    else:
        row.display_name = body.name or row.display_name
        row.role, row.disabled = body.role, body.disabled
        s.commit()
        if body.disabled:
            auth.end_all_sessions(s, username)
        if body.password:
            auth.set_password(s, row, body.password)

    chosen = sites.assign(s, username, body.role,
                          kept if body.sites is None else body.sites)
    audit(s, "save", "user", username, actor=user, role=body.role,
          disabled=body.disabled, sites=chosen)
    s.commit()
    return _user_json(s, row)


@app.post("/users/{username}/password")
def set_user_password(username: str, body: PasswordBody, s: Session = Depends(get_session),
                      user: AppUser = Depends(admin)) -> dict[str, Any]:
    row = _user_row(s, username)
    try:
        auth.set_password(s, row, body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    audit(s, "password", "user", username, actor=user)
    s.commit()
    return {"username": username, "signed_out": True}


@app.post("/users/{username}/sessions/end")
def end_user_sessions(username: str, s: Session = Depends(get_session),
                      user: AppUser = Depends(admin)) -> dict[str, Any]:
    _user_row(s, username)
    auth.end_all_sessions(s, username)
    audit(s, "signout", "user", username, actor=user)
    s.commit()
    return {"username": username, "signed_out": True}


@app.delete("/users/{username}", status_code=204)
def delete_user(username: str, s: Session = Depends(get_session),
                user: AppUser = Depends(admin)) -> Response:
    row = _user_row(s, username)
    if username == user.username:
        raise HTTPException(409, "you can't delete yourself")
    if row.role == "admin" and not _other_admins(s, username):
        raise HTTPException(409, f"{username} is the last administrator")
    auth.end_all_sessions(s, username)
    sites.assign(s, username, row.role, [])
    s.delete(row)
    audit(s, "delete", "user", username, actor=user)
    s.commit()
    return Response(status_code=204)


@app.get("/keys", dependencies=[Depends(admin)])
def list_keys(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [{"id": k.id[:12], "name": k.name, "role": k.role, "agent_id": k.agent_id,
             "sites": k.sites or [],
             "created_by": k.created_by, "created_at": k.created_at,
             "last_used_at": k.last_used_at}
            for k in s.scalars(select(db.ApiToken).order_by(db.ApiToken.created_at.desc()))]


@app.post("/keys", status_code=201)
def make_key(body: KeyBody, s: Session = Depends(get_session),
             user: AppUser = Depends(admin)) -> dict[str, Any]:
    """A key for something that isn't a person. Shown once, here."""
    if body.role not in ("admin", "operator"):
        raise HTTPException(422, "an agent key is made with the agent, not here")
    chosen = sorted(set(body.sites))
    if chosen and body.role == "admin":
        raise HTTPException(422, "an administrator key reaches every site; make an "
                                 "operator key to keep a script to some of them")
    for site_id in chosen:
        site = s.get(db.Site, site_id)
        if site is None:
            raise HTTPException(422, f"no site {site_id!r}")
        if site.archived_at is not None:
            raise HTTPException(422, f"{site.name} is archived; restore it first")
    token = auth.create_token(s, name=body.name, role=body.role, created_by=user.username)
    s.get(db.ApiToken, auth.token_id(token)).sites = chosen
    audit(s, "create", "key", body.name, actor=user, role=body.role, sites=chosen)
    s.commit()
    return {"id": auth.token_id(token)[:12], "name": body.name, "role": body.role,
            "sites": chosen, "token": token}


@app.delete("/keys/{key_id}", status_code=204)
def revoke_key(key_id: str, s: Session = Depends(get_session),
               user: AppUser = Depends(admin)) -> Response:
    rows = [k for k in s.scalars(select(db.ApiToken)) if k.id.startswith(key_id)]
    if not rows:
        raise HTTPException(404, f"no key {key_id!r}")
    for row in rows:
        s.delete(row)
    audit(s, "revoke", "key", key_id, actor=user)
    s.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------- agents

class AgentBody(BaseModel):
    name: str


class PollBody(BaseModel):
    devices: list[str] = []
    accepts: list[Literal["zpl", "pdf"]] = ["zpl"]   # an agent from before pages says nothing


class DoneBody(BaseModel):
    error: str | None = None


def _agent_json(row: db.PrintAgent) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "devices": row.devices,
            "last_seen_at": row.last_seen_at, "created_at": row.created_at,
            "online": _agent_online(row)}


def _agent_online(row: db.PrintAgent) -> bool:
    return bool(row.last_seen_at
                and row.last_seen_at > auth.now() - printers.AGENT_ONLINE)


def _agent_row(s: Session, agent_id: str) -> db.PrintAgent:
    row = s.get(db.PrintAgent, agent_id)
    if row is None:
        raise HTTPException(404, f"no agent {agent_id!r}")
    return row


@app.get("/agents", dependencies=[Depends(admin)])
def list_agents(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [_agent_json(a) for a in s.scalars(
        select(db.PrintAgent).order_by(db.PrintAgent.name))]


@app.get("/agents/{agent_id}", dependencies=[Depends(admin)])
def get_agent(agent_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _agent_json(_agent_row(s, agent_id))


@app.put("/agents/{agent_id}")
def put_agent(agent_id: str, body: AgentBody, s: Session = Depends(get_session),
              user: AppUser = Depends(admin)) -> dict[str, Any]:
    """Making an agent hands back its key. That is the only time anyone sees
    it; only its hash is kept."""
    row = s.get(db.PrintAgent, agent_id)
    fresh = row is None
    row = row or db.PrintAgent(id=agent_id, devices=[])
    row.name = body.name
    s.add(row)
    s.commit()
    out = _agent_json(row)
    if fresh:
        out["token"] = auth.create_token(s, name=f"agent {agent_id}", role="agent",
                                         agent_id=agent_id, created_by=user.username)
    audit(s, "save", "agent", agent_id, actor=user)
    s.commit()
    return out


@app.post("/agents/{agent_id}/token")
def rotate_agent_token(agent_id: str, s: Session = Depends(get_session),
                       user: AppUser = Depends(admin)) -> dict[str, Any]:
    _agent_row(s, agent_id)
    auth.revoke_tokens(s, agent_id=agent_id)
    token = auth.create_token(s, name=f"agent {agent_id}", role="agent",
                              agent_id=agent_id, created_by=user.username)
    audit(s, "rotate", "agent", agent_id, actor=user)
    s.commit()
    return {"id": agent_id, "token": token}


@app.delete("/agents/{agent_id}", status_code=204)
def delete_agent(agent_id: str, s: Session = Depends(get_session),
                 user: AppUser = Depends(admin)) -> Response:
    row = _agent_row(s, agent_id)
    used = s.scalars(select(db.Printer.id).where(
        db.Printer.transport_kind == "agent")).all()
    on_it = [p for p in used
             if (s.get(db.Printer, p).transport_config or {}).get("agent_id") == agent_id]
    if on_it:
        raise HTTPException(409, f"printers still go through {agent_id!r}: {', '.join(on_it)}")
    auth.revoke_tokens(s, agent_id=agent_id)
    for job in s.scalars(select(db.AgentJob).where(db.AgentJob.agent_id == agent_id)):
        s.delete(job)
    s.delete(row)
    audit(s, "delete", "agent", agent_id, actor=user)
    s.commit()
    return Response(status_code=204)


@app.post("/agents/{agent_id}/poll")
def agent_poll(agent_id: str, body: PollBody, s: Session = Depends(get_session),
               token: db.ApiToken = Depends(agent_token)) -> dict[str, Any]:
    """The agent asking for work. Also how it says it is still there."""
    row = _agent_row(s, agent_id)
    row.last_seen_at = auth.now()
    if body.devices:
        row.devices = body.devices
    row.accepts = list(body.accepts)

    stmt = (select(db.AgentJob)
            .where(db.AgentJob.agent_id == agent_id, db.AgentJob.taken_at.is_(None))
            .order_by(db.AgentJob.created_at).limit(20))
    if "pdf" not in body.accepts:
        stmt = stmt.where(db.AgentJob.body.is_(None))
    waiting = s.scalars(stmt).all()
    for job in waiting:
        job.taken_at = auth.now()
    s.commit()
    # a page travels as base64 in the same JSON a label does; the agent is
    # standard library only, and json + base64 are both in it
    return {"agent": agent_id,
            "jobs": [{"id": j.id, "device": j.device, "zpl": j.zpl,
                      **({"pdf": base64.b64encode(j.body).decode()} if j.body else {})}
                     for j in waiting]}


@app.post("/agents/{agent_id}/jobs/{job_id}/done")
def agent_done(agent_id: str, job_id: str, body: DoneBody,
               s: Session = Depends(get_session),
               token: db.ApiToken = Depends(agent_token)) -> dict[str, str]:
    job = s.get(db.AgentJob, job_id)
    if job is None or job.agent_id != agent_id:
        raise HTTPException(404, f"no job {job_id!r} for {agent_id!r}")
    job.done_at, job.error = auth.now(), body.error
    s.commit()
    return {"id": job_id, "status": "failed" if body.error else "printed"}


class ScanRequest(BaseModel):
    network: str
    port: int = 9100


@app.post("/printers/scan")
def scan_printers(body: ScanRequest, s: Session = Depends(get_session),
                 user: AppUser = Depends(admin)) -> dict[str, Any]:
    """Look for printers already on the network. Private ranges only."""
    try:
        found = printers.scan(body.network, port=body.port)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None

    known = {
        (r.transport_config.get("host"), r.transport_config.get("port", 9100)): r.id
        for r in s.scalars(select(db.Printer).where(db.Printer.transport_kind == "tcp"))
    }
    audit(s, "scan", "network", body.network, port=body.port, found=len(found), actor=user)
    s.commit()
    return {
        "scanned": len(found),
        "found": [
            {"host": f.host, "port": f.port, "model": f.model, "firmware": f.firmware,
             "configured_as": known.get((f.host, f.port))}
            for f in found
        ],
    }


@app.post("/printers/{printer_id}/test", status_code=202, dependencies=[Depends(admin)])
def test_printer(printer_id: str, s: Session = Depends(get_session)) -> dict[str, str]:
    p = printers.load(s, printer_id)
    if p is None:
        raise HTTPException(404, f"no printer {printer_id!r}")
    row = s.get(db.Printer, printer_id)
    # ZPL on an office printer comes out as a page of ZPL; it gets a page instead
    test = (pages.render_document(pages.TEST_PAGE, [{}])[0] if row.kind == "page"
            else printers.TEST_LABEL)
    try:
        p.transport.send(test)
    except Exception as exc:
        raise HTTPException(502, f"{printer_id}: {type(exc).__name__}: {exc}") from None
    return {"id": printer_id, "sent": "test page" if row.kind == "page" else "test label"}
