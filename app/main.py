"""The HTTP surface. Thin on purpose — the work lives in the modules beside it."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import models as db
from db.session import get_session

from . import binding, datasources, jobs, preview, printers, zplimport
from .models import Template
from .zpl import RenderError, render_run

app = FastAPI(title="Platen")

WEB = Path(__file__).resolve().parent.parent / "web"
app.mount("/studio/static", StaticFiles(directory=WEB), name="static")


SCREENS = {"print": "print.html", "data": "data.html", "printers": "printers.html",
           "templates": "templates.html", "editor": "editor.html"}


@app.get("/studio/{screen}", include_in_schema=False)
def studio(screen: str) -> FileResponse:
    if screen not in SCREENS:
        raise HTTPException(404, f"no screen {screen!r}")
    return FileResponse(WEB / "studio" / SCREENS[screen], media_type="text/html")


def audit(s: Session, action: str, entity: str, entity_id: str, **detail: Any) -> None:
    s.add(db.AuditLog(action=action, entity=entity, entity_id=entity_id, detail=detail))


# ---------------------------------------------------------------- templates

def _latest_version(row: db.Template) -> db.TemplateVersion | None:
    return row.versions[-1] if row.versions else None


def _as_template(row: db.Template) -> Template:
    latest = _latest_version(row)
    return Template(
        id=row.id, name=row.name, version=latest.version if latest else 0,
        width_mm=row.width_mm, height_mm=row.height_mm, dpi=row.dpi,
        darkness=row.darkness, datasource=row.datasource_id, query=row.query_id,
        elements=row.elements,
    )


def _template_row(s: Session, template_id: str) -> db.Template:
    row = s.get(db.Template, template_id)
    if row is None:
        raise HTTPException(404, f"no template {template_id!r}")
    return row


@app.get("/templates")
def list_templates(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [
        {"id": t.id, "name": t.name, "version": v.version if (v := _latest_version(t)) else 0,
         "size_mm": [t.width_mm, t.height_mm], "dpi": t.dpi, "datasource": t.datasource_id,
         "query": t.query_id, "updated_at": t.updated_at}
        for t in s.scalars(select(db.Template).order_by(db.Template.name))
    ]


class ImportZpl(BaseModel):
    id: str
    name: str
    zpl: str
    dpi: Literal[203, 300, 600] = 203


@app.post("/templates/import", status_code=201)
def import_zpl(body: ImportZpl, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Read a label somebody else wrote. It lands as a draft, never published,
    and the warnings say what didn't survive the trip."""
    if s.get(db.Template, body.id) is not None:
        raise HTTPException(409, f"a template called {body.id!r} already exists; "
                                 "pick another name or delete that one first")
    try:
        result = zplimport.parse(body.zpl, id=body.id, name=body.name, dpi=body.dpi)
    except zplimport.NotZpl as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception as exc:                          # noqa: BLE001 — arbitrary input
        raise HTTPException(422, f"this label could not be read: {exc}") from None

    put_template(body.id, result.template, s)
    audit(s, "import", "template", body.id, elements=len(result.template.elements),
          warnings=result.warnings)
    s.commit()
    return {"id": body.id, "elements": len(result.template.elements),
            "warnings": result.warnings}


@app.get("/templates/{template_id}")
def get_template(template_id: str, s: Session = Depends(get_session)) -> Template:
    return _as_template(_template_row(s, template_id))


@app.put("/templates/{template_id}")
def put_template(template_id: str, template: Template,
                 s: Session = Depends(get_session)) -> Template:
    """Saves the draft. Nothing prints from a draft — see publish."""
    row = s.get(db.Template, template_id) or db.Template(id=template_id)
    row.name = template.name
    row.width_mm, row.height_mm, row.dpi = template.width_mm, template.height_mm, template.dpi
    row.darkness = template.darkness
    row.datasource_id, row.query_id = template.datasource, template.query
    row.elements = [e.model_dump(mode="json") for e in template.elements]
    s.add(row)
    s.commit()
    return _as_template(row)


@app.post("/templates/{template_id}/publish")
def publish_template(template_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Freezes the draft as the next version. Earlier versions are never touched."""
    row = _template_row(s, template_id)
    latest = _latest_version(row)
    version = db.TemplateVersion(
        template=row, version=(latest.version + 1) if latest else 1,
        definition=_as_template(row).model_dump(mode="json"),
    )
    version.definition["version"] = version.version
    s.add(version)
    audit(s, "publish", "template", row.id, version=version.version)
    s.commit()
    return {"id": row.id, "version": version.version, "published_at": version.published_at}


@app.delete("/templates/{template_id}", status_code=204)
def delete_template(template_id: str, s: Session = Depends(get_session)) -> Response:
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
    audit(s, "delete", "template", template_id)
    s.commit()
    return Response(status_code=204)


@app.get("/templates/{template_id}/versions")
def list_versions(template_id: str, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    row = _template_row(s, template_id)
    return [{"version": v.version, "published_at": v.published_at} for v in row.versions]


# -------------------------------------------------------------- data sources

class DataSourceBody(BaseModel):
    name: str
    label: str = ""
    url: str
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
    sql: str
    parameters: list[ParameterBody] = []


class PreviewQuery(BaseModel):
    params: dict[str, Any] = {}
    limit: int = 25


def _datasource_json(s: Session, row: db.DataSource) -> dict[str, Any]:
    queries = s.scalars(
        select(db.SavedQuery.id).where(db.SavedQuery.datasource_id == row.id)
        .order_by(db.SavedQuery.id)
    ).all()
    return {"id": row.id, "name": row.name, "label": row.label,
            "url": datasources.masked(row.url), "pool_size": row.pool_size,
            "queries": list(queries)}


@app.get("/datasources")
def list_datasources(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [_datasource_json(s, d)
            for d in s.scalars(select(db.DataSource).order_by(db.DataSource.name))]


def _datasource_row(s: Session, datasource_id: str) -> db.DataSource:
    row = s.get(db.DataSource, datasource_id)
    if row is None:
        raise HTTPException(404, f"no data source {datasource_id!r}")
    return row


@app.get("/datasources/{datasource_id}")
def get_datasource(datasource_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _datasource_json(s, _datasource_row(s, datasource_id))


@app.get("/datasources/{datasource_id}/reveal")
def reveal_datasource(datasource_id: str, s: Session = Depends(get_session)) -> dict[str, str]:
    """The connection string with its password. Kept apart from the ordinary
    read so it never rides along with a screen that only needs to show it."""
    return {"id": datasource_id, "url": _datasource_row(s, datasource_id).url}


@app.put("/datasources/{datasource_id}")
def put_datasource(datasource_id: str, body: DataSourceBody,
                   s: Session = Depends(get_session)) -> dict[str, Any]:
    row = s.get(db.DataSource, datasource_id)
    url = datasources.unmasked(body.url, row.url if row else None)
    row = row or db.DataSource(id=datasource_id)
    row.name, row.label, row.url, row.pool_size = body.name, body.label, url, body.pool_size
    s.add(row)
    audit(s, "save", "datasource", row.id)
    s.commit()
    return _datasource_json(s, row)


@app.delete("/datasources/{datasource_id}", status_code=204)
def delete_datasource(datasource_id: str, s: Session = Depends(get_session)) -> Response:
    row = _datasource_row(s, datasource_id)
    used = s.scalars(
        select(db.SavedQuery.id).where(db.SavedQuery.datasource_id == row.id)
    ).all()
    if used:
        raise HTTPException(
            409, f"{datasource_id!r} still has saved queries on it: {', '.join(used)}. "
                 "Delete those first.")
    s.delete(row)
    audit(s, "delete", "datasource", datasource_id)
    s.commit()
    return Response(status_code=204)


@app.post("/datasources/{datasource_id}/test")
def test_datasource(datasource_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    ds = datasources.datasource(s, datasource_id)
    if ds is None:
        raise HTTPException(404, f"no data source {datasource_id!r}")
    try:
        return ds.probe()
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.get("/queries")
def list_queries(datasource_id: str | None = None,
                 s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(db.SavedQuery).order_by(db.SavedQuery.name)
    if datasource_id is not None:
        stmt = stmt.where(db.SavedQuery.datasource_id == datasource_id)
    return [{"id": q.id, "name": q.name, "datasource_id": q.datasource_id,
             "parameters": [p.name for p in q.parameters]}
            for q in s.scalars(stmt)]


@app.put("/queries/{query_id}")
def put_query(query_id: str, body: QueryBody, s: Session = Depends(get_session)) -> dict[str, Any]:
    if s.get(db.DataSource, body.datasource_id) is None:
        raise HTTPException(422, f"no data source {body.datasource_id!r}")
    row = s.get(db.SavedQuery, query_id) or db.SavedQuery(id=query_id)
    row.datasource_id, row.name, row.sql = body.datasource_id, body.name, body.sql

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
def get_query(query_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    row = s.get(db.SavedQuery, query_id)
    if row is None:
        raise HTTPException(404, f"no saved query {query_id!r}")
    return {
        "id": row.id, "name": row.name, "datasource_id": row.datasource_id, "sql": row.sql,
        "parameters": [{"name": p.name, "type": p.type, "default": p.default,
                        "ask_at_print": p.ask_at_print, "label": p.label}
                       for p in row.parameters],
    }


@app.get("/queries/{query_id}/columns")
def query_columns(query_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    """The bindable fields, for the editor's field list. No parameters needed."""
    try:
        return {"columns": datasources.columns(s, _query(s, query_id))}
    except HTTPException:
        raise
    except Exception as exc:                          # noqa: BLE001 — the operator wrote the SQL
        raise HTTPException(422, f"{query_id}: {exc}") from None


@app.delete("/queries/{query_id}", status_code=204)
def delete_query(query_id: str, s: Session = Depends(get_session)) -> Response:
    row = s.get(db.SavedQuery, query_id)
    if row is None:
        raise HTTPException(404, f"no saved query {query_id!r}")
    used = s.scalars(select(db.Template.id).where(db.Template.query_id == query_id)).all()
    if used:
        raise HTTPException(
            409, f"{query_id!r} is bound to templates: {', '.join(used)}. "
                 "Point them somewhere else first.")
    s.delete(row)
    audit(s, "delete", "query", query_id)
    s.commit()
    return Response(status_code=204)


def _query(s: Session, query_id: str) -> datasources.SavedQuery:
    q = datasources.saved_query(s, query_id)
    if q is None:
        raise HTTPException(404, f"no saved query {query_id!r}")
    return q


@app.post("/queries/{query_id}/preview")
def preview_query(query_id: str, body: PreviewQuery,
                  s: Session = Depends(get_session)) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        rows = datasources.run(s, _query(s, query_id), body.params, limit=body.limit)
    except HTTPException:
        raise
    except Exception as exc:                          # noqa: BLE001 — the operator wrote the SQL
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
        raise HTTPException(422, f"template {template_id!r} has no published version; publish it first")
    return Template.model_validate(version.definition)


@app.post("/templates/{template_id}/preview.png")
def preview_png(template_id: str, body: RenderPreview,
                s: Session = Depends(get_session)) -> Response:
    t = _preview_template(s, template_id, body.published)
    row = _one_row(s, t, body)
    try:
        return Response(preview.render_png(t, row), media_type="image/png")
    except preview.RenderError as exc:
        raise HTTPException(422, str(exc)) from None


@app.post("/templates/{template_id}/preview.zpl")
def preview_zpl(template_id: str, body: RenderPreview,
                s: Session = Depends(get_session)) -> Response:
    t = _preview_template(s, template_id, body.published)
    row = _one_row(s, t, body)
    try:
        labels, warnings = render_run(t, [row])
    except RenderError as exc:
        raise HTTPException(422, str(exc)) from None
    return Response("\n".join(labels), media_type="text/plain",
                    headers={"x-platen-warnings": "; ".join(warnings)})


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
    except Exception as exc:                          # noqa: BLE001 — the operator wrote the SQL
        raise HTTPException(422, f"{t.query}: {exc}") from None
    if not rows:
        raise HTTPException(422, "the query returned no rows")
    return rows[min(body.record, len(rows) - 1)]


# ---------------------------------------------------------------- print runs

class RunSpec(BaseModel):
    template_id: str
    params: dict[str, Any] = {}
    copies: int = 1
    start_at: int = 1
    skip_rows: list[int] = []      # 1-based record numbers, as the operator sees them


class NewRun(RunSpec):
    printer_id: str


class Prepared(BaseModel):
    version_id: int
    version: int
    records: int
    labels: list[str]
    warnings: list[str]


def _prepare(s: Session, body: RunSpec) -> Prepared:
    """Pull the rows and render every label. Shared by the dry run and the
    real one so the operator's check and the print never disagree."""
    version = _latest_version(_template_row(s, body.template_id))
    if version is None:
        raise HTTPException(422, f"template {body.template_id!r} has no published version; publish it first")
    t = Template.model_validate(version.definition)

    rows = datasources.run(s, _query(s, t.query), body.params) if t.query else [{}]
    skip = set(body.skip_rows)
    rows = [r for i, r in enumerate(rows, start=1) if i >= body.start_at and i not in skip]

    try:
        labels, warnings = render_run(t, rows, copies=body.copies)
    except RenderError as exc:
        raise HTTPException(422, str(exc)) from None
    return Prepared(version_id=version.id, version=version.version, records=len(rows),
                    labels=labels, warnings=warnings)


@app.post("/runs/check")
def check_run(body: RunSpec, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Everything a run would do short of writing it down or queueing it."""
    started = time.perf_counter()
    p = _prepare(s, body)
    return {"records": p.records, "labels": len(p.labels), "warnings": p.warnings,
            "template_version": p.version,
            "elapsed_ms": round((time.perf_counter() - started) * 1000)}


def _run_json(run: db.PrintRun) -> dict[str, Any]:
    return {
        "id": run.id, "status": run.status, "printed": run.printed, "total": run.total,
        "error": run.error, "template_id": run.template_version.template_id,
        "template_version": run.template_version.version, "printer_id": run.printer_id,
        "warnings": [f"row {w.row_no}: {w.message}" if w.row_no else w.message
                     for w in run.warnings],
        "created_at": run.created_at, "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


@app.post("/runs", status_code=202)
def create_run(body: NewRun, s: Session = Depends(get_session)) -> dict[str, Any]:
    if s.get(db.Printer, body.printer_id) is None:
        raise HTTPException(404, f"no printer {body.printer_id!r}")

    # every label renders here, before a run row exists, let alone a job
    p = _prepare(s, body)

    run = db.PrintRun(
        id=f"JOB-{uuid.uuid4().hex[:6].upper()}", template_version_id=p.version_id,
        printer_id=body.printer_id, params=body.params, copies=body.copies, total=len(p.labels),
        labels=[db.RunLabel(seq=i, zpl=z) for i, z in enumerate(p.labels, start=1)],
        warnings=[_warning(w) for w in p.warnings],
    )
    s.add(run)
    audit(s, "create", "run", run.id, template_id=body.template_id, template_version=p.version,
          printer_id=body.printer_id, labels=len(p.labels), skip_rows=body.skip_rows)
    s.commit()

    try:
        jobs.enqueue(run.id)
    except Exception as exc:                          # noqa: BLE001
        run.status, run.error = "failed", f"could not queue: {type(exc).__name__}: {exc}"
        s.commit()
        raise HTTPException(503, run.error) from None
    return {"id": run.id, "labels": run.total, "warnings": p.warnings,
            "template_version": p.version}


def _warning(text: str) -> db.RunWarning:
    """render_run prefixes warnings with "row N: "; the table keeps N as a column."""
    head, _, rest = text.partition(": ")
    if head.startswith("row ") and head[4:].isdigit():
        return db.RunWarning(row_no=int(head[4:]), message=rest)
    return db.RunWarning(row_no=None, message=text)


def _run_row(s: Session, run_id: str) -> db.PrintRun:
    run = s.get(db.PrintRun, run_id)
    if run is None:
        raise HTTPException(404, f"no run {run_id!r}")
    return run


@app.get("/runs")
def list_runs(s: Session = Depends(get_session), limit: int = 50) -> list[dict[str, Any]]:
    runs = s.scalars(select(db.PrintRun).order_by(db.PrintRun.created_at.desc()).limit(limit))
    return [_run_json(r) for r in runs]


@app.get("/runs/{run_id}")
def get_run(run_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _run_json(_run_row(s, run_id))


@app.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(run_id: str, s: Session = Depends(get_session)) -> dict[str, str]:
    run = _run_row(s, run_id)
    if run.status in ("done", "failed", "cancelled"):
        return {"id": run_id, "status": run.status}
    audit(s, "cancel", "run", run_id)
    jobs.cancel(s, run_id)
    return {"id": run_id, "status": "cancelling"}


# ------------------------------------------------------------------ printers

class PrinterBody(BaseModel):
    name: str
    model: str = ""
    dpi: int = 203
    transport: dict[str, Any]


def _printer_json(row: db.Printer, online: bool | None) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "model": row.model, "dpi": row.dpi,
            "transport": {"kind": row.transport_kind, **row.transport_config},
            "online": online}


def _probe(row: db.Printer) -> bool | None:
    try:
        return printers.from_row(row).transport.probe()
    except Exception:                                 # noqa: BLE001 — unreachable is not online
        return False


@app.get("/printers")
def list_printers(probe: bool = True,
                  s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Probing talks to every printer, and an offline one only answers when it
    times out — so they are asked all at once rather than one after another."""
    rows = list(s.scalars(select(db.Printer).order_by(db.Printer.name)))
    if not probe:
        return [_printer_json(r, None) for r in rows]
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(rows)))) as pool:
        states = list(pool.map(_probe, rows))
    return [_printer_json(r, online) for r, online in zip(rows, states)]


def _printer_row(s: Session, printer_id: str) -> db.Printer:
    row = s.get(db.Printer, printer_id)
    if row is None:
        raise HTTPException(404, f"no printer {printer_id!r}")
    return row


@app.get("/printers/{printer_id}")
def get_printer(printer_id: str, probe: bool = False,
                s: Session = Depends(get_session)) -> dict[str, Any]:
    row = _printer_row(s, printer_id)
    return _printer_json(row, _probe(row) if probe else None)


@app.delete("/printers/{printer_id}", status_code=204)
def delete_printer(printer_id: str, s: Session = Depends(get_session)) -> Response:
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
    audit(s, "delete", "printer", printer_id)
    s.commit()
    return Response(status_code=204)


@app.put("/printers/{printer_id}")
def put_printer(printer_id: str, body: PrinterBody,
                s: Session = Depends(get_session)) -> dict[str, Any]:
    config = dict(body.transport)
    kind = config.pop("kind", None)
    if kind is None:
        raise HTTPException(422, "transport.kind is required: tcp, cups or agent")
    try:
        printers.build(kind, config)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, f"transport: {exc}") from None
    row = s.get(db.Printer, printer_id) or db.Printer(id=printer_id)
    row.name, row.model, row.dpi = body.name, body.model, body.dpi
    row.transport_kind, row.transport_config = kind, config
    s.add(row)
    audit(s, "save", "printer", row.id, transport=kind)
    s.commit()
    return _printer_json(row, None)


@app.post("/printers/{printer_id}/test", status_code=202)
def test_printer(printer_id: str, s: Session = Depends(get_session)) -> dict[str, str]:
    p = printers.load(s, printer_id)
    if p is None:
        raise HTTPException(404, f"no printer {printer_id!r}")
    try:
        p.transport.send(printers.TEST_LABEL)
    except Exception as exc:                          # noqa: BLE001
        raise HTTPException(502, f"{printer_id}: {type(exc).__name__}: {exc}") from None
    return {"id": printer_id, "sent": "test label"}
