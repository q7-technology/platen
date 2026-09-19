"""The HTTP surface. Thin on purpose — the work lives in the modules beside it."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from . import datasources, jobs, preview, printers
from .models import Template
from .zpl import RenderError, render_run

app = FastAPI(title="Platen")

TEMPLATES: dict[str, Template] = {}          # swap for a real table


# ---------------------------------------------------------------- templates

@app.get("/templates")
def list_templates() -> list[dict[str, Any]]:
    return [
        {"id": t.id, "name": t.name, "version": t.version,
         "size_mm": [t.width_mm, t.height_mm], "dpi": t.dpi}
        for t in TEMPLATES.values()
    ]


@app.get("/templates/{template_id}")
def get_template(template_id: str) -> Template:
    try:
        return TEMPLATES[template_id]
    except KeyError:
        raise HTTPException(404, "no such template") from None


@app.put("/templates/{template_id}")
def put_template(template_id: str, template: Template) -> Template:
    old = TEMPLATES.get(template_id)
    template.version = (old.version + 1) if old else 1
    TEMPLATES[template_id] = template
    return template


# -------------------------------------------------------------- data sources

class PreviewQuery(BaseModel):
    params: dict[str, Any] = {}
    limit: int = 25


@app.get("/datasources")
def list_datasources() -> list[dict[str, str]]:
    return [{"name": d.name, "label": d.label} for d in datasources.REGISTRY.values()]


@app.post("/datasources/{name}/test")
def test_datasource(name: str) -> dict[str, Any]:
    try:
        return datasources.REGISTRY[name].probe()
    except KeyError:
        raise HTTPException(404, "no such data source") from None
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.post("/queries/{query_name}/preview")
def preview_query(query_name: str, body: PreviewQuery) -> dict[str, Any]:
    q = datasources.QUERIES.get(query_name)
    if q is None:
        raise HTTPException(404, "no such query")
    rows = datasources.run(q, body.params, limit=body.limit)
    return {"rows": rows, "fields": datasources.describe(rows), "count": len(rows)}


# ------------------------------------------------------------------ previews

class RenderPreview(BaseModel):
    params: dict[str, Any] = {}
    record: int = 0


@app.post("/templates/{template_id}/preview.png")
def preview_png(template_id: str, body: RenderPreview) -> Response:
    t = get_template(template_id)
    row = _one_row(t, body.params, body.record)
    return Response(preview.render_png(t, row), media_type="image/png")


@app.post("/templates/{template_id}/preview.zpl")
def preview_zpl(template_id: str, body: RenderPreview) -> Response:
    t = get_template(template_id)
    row = _one_row(t, body.params, body.record)
    labels, warnings = render_run(t, [row])
    return Response("\n".join(labels), media_type="text/plain",
                    headers={"x-platen-warnings": "; ".join(warnings)})


def _one_row(t: Template, params: dict[str, Any], index: int) -> dict[str, Any]:
    if not t.query:
        return {}
    rows = datasources.run(datasources.QUERIES[t.query], params, limit=index + 1)
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


@app.post("/runs", status_code=202)
def create_run(body: NewRun) -> dict[str, Any]:
    t = get_template(body.template_id)
    if body.printer_id not in printers.PRINTERS:
        raise HTTPException(404, "no such printer")

    rows = datasources.run(datasources.QUERIES[t.query], body.params) if t.query else [{}]
    rows = rows[body.start_at - 1:]

    try:
        labels, warnings = render_run(t, rows, copies=body.copies)
    except RenderError as exc:
        # nothing has been sent anywhere; the operator gets a real message
        raise HTTPException(422, str(exc)) from None

    run = jobs.Run(id=f"JOB-{uuid.uuid4().hex[:6].upper()}", template_id=t.id,
                   printer_id=body.printer_id, labels=labels, warnings=warnings)
    jobs.enqueue(run)
    return {"id": run.id, "labels": run.total, "warnings": warnings}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    try:
        run = jobs.load(run_id)
    except KeyError:
        raise HTTPException(404, "no such run") from None
    return {"id": run.id, "status": run.status, "printed": run.printed,
            "total": run.total, "error": run.error, "warnings": run.warnings}


@app.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(run_id: str) -> dict[str, str]:
    jobs.cancel(run_id)
    return {"id": run_id, "status": "cancelling"}


# ------------------------------------------------------------------ printers

@app.get("/printers")
def list_printers() -> list[dict[str, Any]]:
    return [
        {"id": p.id, "name": p.name, "model": p.model, "dpi": p.dpi,
         "online": p.transport.probe()}
        for p in printers.PRINTERS.values()
    ]


@app.post("/printers/{printer_id}/test", status_code=202)
def test_printer(printer_id: str) -> dict[str, str]:
    p = printers.PRINTERS.get(printer_id)
    if p is None:
        raise HTTPException(404, "no such printer")
    p.transport.send(printers.TEST_LABEL)
    return {"id": printer_id, "sent": "test label"}
