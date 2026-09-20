"""The HTTP surface. Thin on purpose — the work lives in the modules beside it."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import models as db
from db.session import get_session

from . import datasources, jobs, preview, printers
from .models import Template
from .zpl import RenderError, render_run

app = FastAPI(title="Platen")


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
         "size_mm": [t.width_mm, t.height_mm], "dpi": t.dpi, "updated_at": t.updated_at}
        for t in s.scalars(select(db.Template).order_by(db.Template.name))
    ]


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


@app.get("/datasources")
def list_datasources(s: Session = Depends(get_session)) -> list[dict[str, str]]:
    return [{"id": d.id, "name": d.name, "label": d.label}
            for d in s.scalars(select(db.DataSource).order_by(db.DataSource.name))]


@app.put("/datasources/{datasource_id}")
def put_datasource(datasource_id: str, body: DataSourceBody,
                   s: Session = Depends(get_session)) -> dict[str, str]:
    row = s.get(db.DataSource, datasource_id) or db.DataSource(id=datasource_id)
    row.name, row.label, row.url, row.pool_size = body.name, body.label, body.url, body.pool_size
    s.add(row)
    s.commit()
    return {"id": row.id, "name": row.name, "label": row.label}


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
def list_queries(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [{"id": q.id, "name": q.name, "datasource_id": q.datasource_id,
             "parameters": [p.name for p in q.parameters]}
            for q in s.scalars(select(db.SavedQuery).order_by(db.SavedQuery.name))]


@app.put("/queries/{query_id}")
def put_query(query_id: str, body: QueryBody, s: Session = Depends(get_session)) -> dict[str, Any]:
    if s.get(db.DataSource, body.datasource_id) is None:
        raise HTTPException(422, f"no data source {body.datasource_id!r}")
    row = s.get(db.SavedQuery, query_id) or db.SavedQuery(id=query_id)
    row.datasource_id, row.name, row.sql = body.datasource_id, body.name, body.sql
    row.parameters = [
        db.QueryParameter(name=p.name, type=p.type, label=p.label, position=i,
                          default=None if p.default is None else str(p.default),
                          ask_at_print=p.ask_at_print)
        for i, p in enumerate(body.parameters)
    ]
    s.add(row)
    s.commit()
    return {"id": row.id, "name": row.name, "parameters": [p.name for p in row.parameters]}


def _query(s: Session, query_id: str) -> datasources.SavedQuery:
    q = datasources.saved_query(s, query_id)
    if q is None:
        raise HTTPException(404, f"no saved query {query_id!r}")
    return q


@app.post("/queries/{query_id}/preview")
def preview_query(query_id: str, body: PreviewQuery,
                  s: Session = Depends(get_session)) -> dict[str, Any]:
    rows = datasources.run(s, _query(s, query_id), body.params, limit=body.limit)
    return {"rows": rows, "fields": datasources.describe(rows), "count": len(rows)}


# ------------------------------------------------------------------ previews

class RenderPreview(BaseModel):
    params: dict[str, Any] = {}
    record: int = 0


@app.post("/templates/{template_id}/preview.png")
def preview_png(template_id: str, body: RenderPreview,
                s: Session = Depends(get_session)) -> Response:
    t = _as_template(_template_row(s, template_id))
    row = _one_row(s, t, body.params, body.record)
    return Response(preview.render_png(t, row), media_type="image/png")


@app.post("/templates/{template_id}/preview.zpl")
def preview_zpl(template_id: str, body: RenderPreview,
                s: Session = Depends(get_session)) -> Response:
    t = _as_template(_template_row(s, template_id))
    row = _one_row(s, t, body.params, body.record)
    try:
        labels, warnings = render_run(t, [row])
    except RenderError as exc:
        raise HTTPException(422, str(exc)) from None
    return Response("\n".join(labels), media_type="text/plain",
                    headers={"x-platen-warnings": "; ".join(warnings)})


def _one_row(s: Session, t: Template, params: dict[str, Any], index: int) -> dict[str, Any]:
    if not t.query:
        return {}
    rows = datasources.run(s, _query(s, t.query), params, limit=index + 1)
    if not rows:
        raise HTTPException(422, "the query returned no rows")
    return rows[min(index, len(rows) - 1)]


# ---------------------------------------------------------------- print runs

class NewRun(BaseModel):
    template_id: str
    printer_id: str
    params: dict[str, Any] = {}
    copies: int = 1
    start_at: int = 1


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
    version = _latest_version(_template_row(s, body.template_id))
    if version is None:
        raise HTTPException(422, f"template {body.template_id!r} has no published version; publish it first")
    if s.get(db.Printer, body.printer_id) is None:
        raise HTTPException(404, f"no printer {body.printer_id!r}")
    t = Template.model_validate(version.definition)

    rows = datasources.run(s, _query(s, t.query), body.params) if t.query else [{}]
    rows = rows[body.start_at - 1:]

    # every label renders here, before a run row exists, let alone a job
    try:
        labels, warnings = render_run(t, rows, copies=body.copies)
    except RenderError as exc:
        raise HTTPException(422, str(exc)) from None

    run = db.PrintRun(
        id=f"JOB-{uuid.uuid4().hex[:6].upper()}", template_version=version,
        printer_id=body.printer_id, params=body.params, copies=body.copies, total=len(labels),
        labels=[db.RunLabel(seq=i, zpl=z) for i, z in enumerate(labels, start=1)],
        warnings=[_warning(w) for w in warnings],
    )
    s.add(run)
    audit(s, "create", "run", run.id, template_id=t.id, template_version=version.version,
          printer_id=body.printer_id, labels=len(labels))
    s.commit()

    try:
        jobs.enqueue(run.id)
    except Exception as exc:                          # noqa: BLE001
        run.status, run.error = "failed", f"could not queue: {type(exc).__name__}: {exc}"
        s.commit()
        raise HTTPException(503, run.error) from None
    return {"id": run.id, "labels": run.total, "warnings": warnings,
            "template_version": version.version}


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


@app.get("/printers")
def list_printers(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    out = []
    for row in s.scalars(select(db.Printer).order_by(db.Printer.name)):
        p = printers.from_row(row)
        out.append({"id": p.id, "name": p.name, "model": p.model, "dpi": p.dpi,
                    "transport": row.transport_kind, "online": p.transport.probe()})
    return out


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
    s.commit()
    return {"id": row.id, "name": row.name, "model": row.model, "dpi": row.dpi, "transport": kind}


@app.post("/printers/{printer_id}/test", status_code=202)
def test_printer(printer_id: str, s: Session = Depends(get_session)) -> dict[str, str]:
    p = printers.load(s, printer_id)
    if p is None:
        raise HTTPException(404, f"no printer {printer_id!r}")
    p.transport.send(printers.TEST_LABEL)
    return {"id": printer_id, "sent": "test label"}
