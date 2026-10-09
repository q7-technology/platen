# Print jobs from Simple WMS — implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Simple WMS print jobs print in Platen on their own, and Platen tells the WMS `printed` or `failed`.

**Architecture:** A new `POST /intake/wms` in Platen takes the WMS body as it is, renders from the inline `data` through the same `_prepare` the `/runs` route uses, and queues a normal run marked with the WMS `job_id`. When such a run settles, the worker queues `report_to_wms`, which POSTs the outcome to the WMS. The WMS worker gains one bearer header. Design: `docs/plans/2026-10-09-wms-intake-design.md`.

**Tech stack:** FastAPI, SQLAlchemy 2, Alembic, RQ + Redis, httpx, pytest (SQLite + fakeredis). Simple WMS: FastAPI, pydantic-settings, pytest against Postgres.

**Branches:** `wms-intake` in `platen/`, `platen-key` in `simple-wms/`. Neither repo commits unless the user asks (both CLAUDE.md files), so there are no commit steps; each task ends with the suite green.

**Platen test command:** `cd platen && .venv/bin/pytest -q` (one file: `.venv/bin/pytest tests/test_wms_intake.py -q`).

---

### Task 1: Settings for the callback

**Files:** Modify `platen/app/settings.py`, `platen/.env.example`

**Step 1:** Add two fields to `Settings` after `secure_cookies`:

```python
    # Where the Simple WMS is, and the key Platen calls it back with. No URL,
    # no callback: a run the WMS asked for still prints.
    wms_url: str
    wms_key: str
```

and in `from_env`:

```python
            wms_url=os.environ.get("WMS_URL", "").strip(),
            wms_key=os.environ.get("WMS_KEY", "").strip(),
```

**Step 2:** Append to `.env.example`:

```
# Simple WMS: where to say a print job it sent came out (or didn't), and a
# WMS API key with the printing:write scope. Leave empty without a WMS.
WMS_URL=
WMS_KEY=
```

**Step 3:** `.venv/bin/pytest -q` — all pass (nothing reads them yet).

---

### Task 2: `print_run.wms_job_id`

**Files:** Modify `platen/db/models.py` (class `PrintRun`), create `platen/db/migrations/versions/0013_wms_intake.py`

**Step 1:** In `PrintRun`, after `move_to`:

```python
    # the Simple WMS job this run prints, so a resend finds it and the WMS is told
    wms_job_id: Mapped[str | None] = mapped_column(String(36), unique=True, index=True)
```

**Step 2:** Migration:

```python
"""Print jobs from Simple WMS

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("print_run", sa.Column("wms_job_id", sa.String(36)))
    op.create_index("ix_print_run_wms_job_id", "print_run", ["wms_job_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_print_run_wms_job_id", table_name="print_run")
    with op.batch_alter_table("print_run") as batch:
        batch.drop_column("wms_job_id")
```

**Step 3:** `.venv/bin/pytest -q` — all pass (includes any model/migration drift check).

---

### Task 3: Let a run render from rows it was handed

Pure refactor: `/runs` behaves exactly as before.

**Files:** Modify `platen/app/main.py` (`_prepare_pages`, `_prepare`, `create_run`)

**Step 1:** `_prepare_pages(s, body, version, records=None)` and `_prepare(s, body, records=None)`. In both, replace the `rows = …` line with:

```python
    rows = records if records is not None else (
        datasources.run(s, _query(s, t.query), body.params) if t.query else [{}])
```

and in `_prepare` pass it on: `return _prepare_pages(s, body, version, records)`.

**Step 2:** Split `create_run`. Everything after the 404 check moves into `_queue_run`; the local `rows` (labels) is renamed `labels_` to avoid the parameter:

```python
def _queue_run(s: Session, user: AppUser, printer: db.Printer, spec: RunSpec, *,
               records: list[dict[str, Any]] | None = None,
               wms_job_id: str | None = None, **detail: Any) -> dict[str, Any]:
    """Check the site, follow a detour, render every label and queue the run.
    Shared by /runs and the WMS intake so the two can't drift apart."""
    _may_use(s, user, printer)
    asked_for = printer
    # … detour block unchanged …
    p = _prepare(s, spec, records)
    # … kind check unchanged …
    # … run_id, labels_ built as before …
    run = db.PrintRun(
        id=run_id, template_version_id=p.version_id,
        printer_id=printer.id, params=spec.params, copies=spec.copies,
        pause_between=spec.pause_between, total=len(labels_),
        labels=labels_, wms_job_id=wms_job_id,
        warnings=[_warning(w) for w in p.warnings],
    )
    s.add(run)
    audit(s, "create", "run", run.id, template_id=spec.template_id, template_version=p.version,
          printer_id=printer.id, asked_for=asked_for.id, labels=len(labels_), kind=p.kind,
          skip_rows=spec.skip_rows, actor=user, **detail)
    # … commit, enqueue, events, return dict unchanged …


@app.post("/runs", status_code=202)
def create_run(body: NewRun, s: Session = Depends(get_session),
               user: AppUser = Depends(current_user)) -> dict[str, Any]:
    printer = s.get(db.Printer, body.printer_id)
    if printer is None:
        raise HTTPException(404, f"no printer {body.printer_id!r}")
    return _queue_run(s, user, printer, body)
```

**Step 3:** `.venv/bin/pytest -q` — all pass, unchanged.

---

### Task 4: `POST /intake/wms`

**Files:** Modify `platen/app/main.py`; create `platen/tests/test_wms_intake.py`

**Step 1: Failing tests**

```python
"""Print jobs sent by Simple WMS: a template name, a printer, and the data."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import jobs
from app.main import _wms_records, app
from db import session as dbsession
from db.models import PrintRun, RunLabel
from tests.test_pages import office, orders  # noqa: F401  (fixtures)
from tests.test_sites import two_sites  # noqa: F401  (fixture)

CARTON = {"consignee_name": "Acme Auto Parts", "consignment_no": "7MB9042199", "logo": None}


def _job(**over) -> dict:
    return {"job_id": str(uuid.uuid4()), "template": "carton", "version": "v3",
            "printer": "dock", "copies": 1,
            "reference": {"type": "delivery", "ref": "0080012345"}, "data": CARTON, **over}


def _key(admin: TestClient, **body) -> TestClient:
    token = admin.post("/keys", json={"name": "wms", "role": "operator", **body}).json()["token"]
    return TestClient(app, headers={"authorization": f"Bearer {token}"})


def test_a_wms_job_prints_from_its_own_data(seeded, queue):
    r = _key(seeded).post("/intake/wms", json=_job())
    assert r.status_code == 202, r.text
    assert r.json()["labels"] == 1 and r.json()["duplicate"] is False
    assert queue.count == 1
    with dbsession.SessionLocal() as s:
        label = s.scalars(select(RunLabel)).one()
        assert "Acme Auto Parts" in label.zpl and "7MB9042199" in label.zpl


def test_the_printer_is_found_by_its_name_as_well(seeded):
    assert _key(seeded).post("/intake/wms", json=_job(printer=" dock 1 ")).json()["printer_id"] == "dock"


def test_a_resent_job_prints_once(seeded, queue):
    c, job = _key(seeded), _job()
    first = c.post("/intake/wms", json=job).json()
    again = c.post("/intake/wms", json=job)
    assert again.status_code == 202
    assert again.json()["id"] == first["id"] and again.json()["duplicate"] is True
    assert queue.count == 1


def test_a_job_that_never_reached_the_queue_gets_another_go(seeded, queue, monkeypatch):
    c, job = _key(seeded), _job()
    real = jobs.enqueue
    monkeypatch.setattr(jobs, "enqueue", lambda run_id: (_ for _ in ()).throw(ConnectionError("redis")))
    assert c.post("/intake/wms", json=job).status_code == 503
    monkeypatch.setattr(jobs, "enqueue", real)
    again = c.post("/intake/wms", json=job)
    assert again.status_code == 202 and queue.count == 1
    assert seeded.get(f"/runs/{again.json()['id']}").json()["status"] == "queued"


def test_what_platen_does_not_have_is_named(seeded):
    c = _key(seeded)
    r = c.post("/intake/wms", json=_job(template="pallet-label"))
    assert r.status_code == 422 and "pallet-label" in r.json()["detail"]
    r = c.post("/intake/wms", json=_job(printer="Packing bench 9"))
    assert r.status_code == 422 and "Packing bench 9" in r.json()["detail"]
    r = c.post("/intake/wms", json=_job(data={"consignee_name": "Acme"}))
    assert r.status_code == 422 and "consignment_no" in r.json()["detail"]


def test_a_key_kept_to_one_site_prints_only_there(two_sites):  # noqa: F811
    c = _key(two_sites, sites=["bal"])
    assert c.post("/intake/wms", json=_job(printer="gee-despatch")).status_code == 403
    assert c.post("/intake/wms", json=_job(printer="bal-despatch")).status_code == 202


def test_nobody_signed_in_is_refused(anon):
    assert anon.post("/intake/wms", json=_job()).status_code == 401


def test_a_page_gets_a_row_per_line_with_the_header_beside_it():
    data = {"order_no": "SO-9", "customer": "IGA", "lines": [
        {"sku": "A", "qty": 1}, {"sku": "B", "qty": 2}]}
    assert _wms_records("page", data) == [
        {"order_no": "SO-9", "customer": "IGA", "sku": "A", "qty": 1},
        {"order_no": "SO-9", "customer": "IGA", "sku": "B", "qty": 2}]
    assert _wms_records("label", data) == [data]
    assert _wms_records("page", {"order_no": "SO-9"}) == [{"order_no": "SO-9"}]


def test_a_packing_slip_from_the_wms_is_one_document(office):  # noqa: F811
    r = _key(office).post("/intake/wms", json=_job(
        template="slip", printer="laser",
        data={"order_no": "SO-9", "customer": "IGA", "lines": [
            {"sku": "A", "descr": "Wipers", "qty": 1}, {"sku": "B", "descr": "Fluid", "qty": 2}]}))
    assert r.status_code == 202, r.text
    assert r.json()["labels"] == 1
```

Run `.venv/bin/pytest tests/test_wms_intake.py -q` → fails on the import of `_wms_records`.

**Step 2: Implement** in `main.py`, after `cancel_run`. Add `Field` to the pydantic import and `from sqlalchemy.exc import IntegrityError`.

```python
# ------------------------------------------------------------------ simple wms

class WmsJob(BaseModel):
    """What Simple WMS sends: its template name, a printer, and the data."""
    job_id: uuid.UUID
    template: str
    version: str = ""
    printer: str
    copies: int = Field(default=1, ge=1, le=99)
    reference: dict[str, Any] = {}
    data: dict[str, Any] = {}


def _wms_printer(s: Session, asked: str) -> db.Printer:
    printer = s.get(db.Printer, asked) or s.scalars(
        select(db.Printer).where(func.lower(db.Printer.name) == asked.strip().lower())).first()
    if printer is None:
        raise HTTPException(422, f"printer: Platen has no printer with the id or name {asked!r}; "
                                 "add it on the printers screen or fix the WMS print point")
    return printer


def _wms_records(kind: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """A label is one record. A page lists the lines, each carrying the
    header beside it, so a table lists them and the heading still binds."""
    lines = data.get("lines")
    if kind != "page" or not isinstance(lines, list) or not lines:
        return [data]
    header = {k: v for k, v in data.items() if k != "lines"}
    return [{**header, **line} if isinstance(line, dict) else dict(header) for line in lines]


def _wms_seen(s: Session, run: db.PrintRun) -> dict[str, Any]:
    """A resent job prints nothing new. One that never reached the queue
    (Redis was down, the WMS got a 503) is put on it now."""
    if run.status == "failed" and run.attempts == 0 and run.printed == 0:
        run.status, run.error, run.finished_at = "queued", None, None
        s.commit()
        jobs.enqueue(run.id)
        events.run_changed(run)
    return {"id": run.id, "labels": run.total, "warnings": [], "printer_id": run.printer_id,
            "duplicate": True}


@app.post("/intake/wms", status_code=202)
def wms_intake(body: WmsJob, s: Session = Depends(get_session),
               user: AppUser = Depends(current_user)) -> dict[str, Any]:
    """A print job from Simple WMS. The template is the one whose id is the
    WMS template name; its saved query isn't run, the data comes with the job."""
    job_id = str(body.job_id)
    seen = s.scalar(select(db.PrintRun).where(db.PrintRun.wms_job_id == job_id))
    if seen is not None:
        return _wms_seen(s, seen)
    template = s.get(db.Template, body.template)
    if template is None:
        raise HTTPException(422, f"template: Platen has no template {body.template!r}; "
                                 "make one with that id and publish it")
    printer = _wms_printer(s, body.printer)
    spec = RunSpec(template_id=template.id, copies=body.copies)
    try:
        out = _queue_run(s, user, printer, spec, records=_wms_records(template.kind, body.data),
                         wms_job_id=job_id, wms_version=body.version,
                         wms_reference=body.reference)
    except IntegrityError:                  # the same job, sent twice at once
        s.rollback()
        return _wms_seen(s, s.scalar(select(db.PrintRun).where(db.PrintRun.wms_job_id == job_id)))
    return {**out, "duplicate": False}
```

Note: `_queue_run`'s enqueue failure path already marks the run `failed` with `attempts == 0` and answers 503, which is what `_wms_seen` picks up.

**Step 3:** `.venv/bin/pytest tests/test_wms_intake.py -q` → all pass. Then `.venv/bin/pytest -q` (the route-table auth test in `tests/test_auth.py` must still pass).

---

### Task 5: Tell the WMS how it went

**Files:** Modify `platen/app/jobs.py`, `platen/app/main.py` (`cancel_run`); append to `platen/tests/test_wms_intake.py`

**Step 1: Failing tests** (add `import dataclasses, logging, pytest` and `from app import settings as settings_module` at the top):

```python
def _queued(queue) -> list[str]:
    return [j.func_name for j in queue.jobs]


def _printed_by_platen(seeded, **job) -> str:
    return _key(seeded).post("/intake/wms", json=_job(**job)).json()["id"]


def test_a_finished_run_queues_the_report(seeded, queue):
    run_id = _printed_by_platen(seeded)
    jobs.print_run(run_id)
    assert _queued(queue).count("app.jobs.report_to_wms") == 1


def test_a_run_from_the_studio_reports_to_nobody(seeded, queue):
    run_id = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                        "params": {"despatch_date": "2026-09-18"}}).json()["id"]
    jobs.print_run(run_id)
    assert "app.jobs.report_to_wms" not in _queued(queue)


def test_a_failed_run_queues_the_report(seeded, queue, transport):
    run_id = _printed_by_platen(seeded)
    transport.after_send = lambda n: (_ for _ in ()).throw(OSError("stopped answering"))
    with pytest.raises(OSError):
        jobs.print_run(run_id)
    assert "app.jobs.report_to_wms" in _queued(queue)


class Wms:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.status = 200

    def post(self, url, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return type("R", (), {"status_code": self.status})()


@pytest.fixture
def wms(monkeypatch) -> Wms:
    w = Wms()
    monkeypatch.setattr(jobs.httpx, "post", w.post)
    monkeypatch.setattr(settings_module, "settings", dataclasses.replace(
        settings_module.settings, wms_url="http://wms.test/", wms_key="wms_live_secret"))
    return w


def _settle(run_id: str, status: str, error: str | None = None) -> None:
    with dbsession.SessionLocal() as s:
        run = s.get(PrintRun, run_id)
        run.status, run.error = status, error
        s.commit()


@pytest.mark.parametrize("status,error,sent", [
    ("done", None, {"status": "printed"}),
    ("failed", "OSError: stopped answering", {"status": "failed",
                                               "message": "OSError: stopped answering"}),
    ("cancelled", None, {"status": "failed", "message": "cancelled in Platen"}),
])
def test_the_wms_hears_how_it_went(seeded, wms, caplog, status, error, sent):
    job = _job()
    run_id = _key(seeded).post("/intake/wms", json=job).json()["id"]
    _settle(run_id, status, error)
    with caplog.at_level(logging.DEBUG):
        jobs.report_to_wms(run_id)
    call = wms.calls[-1]
    assert call["url"] == f"http://wms.test/v1/print-jobs/{job['job_id']}/status"
    assert call["json"] == sent
    assert call["headers"]["Authorization"] == "Bearer wms_live_secret"
    assert "wms_live_secret" not in caplog.text


def test_a_wms_that_refuses_is_tried_again(seeded, wms):
    run_id = _printed_by_platen(seeded)
    _settle(run_id, "done")
    wms.status = 503
    with pytest.raises(RuntimeError, match="503"):
        jobs.report_to_wms(run_id)


def test_without_a_wms_url_nothing_is_sent(seeded, monkeypatch):
    sent = []
    monkeypatch.setattr(jobs.httpx, "post", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(settings_module, "settings",
                        dataclasses.replace(settings_module.settings, wms_url=""))
    run_id = _printed_by_platen(seeded)
    _settle(run_id, "done")
    jobs.report_to_wms(run_id)
    assert sent == []
```

Run → fails: `report_to_wms` not defined.

**Step 2: Implement** in `jobs.py` (add `import httpx`):

```python
SETTLED = ("done", "failed", "cancelled")

# A WMS that is down for a minute, or for the afternoon, still hears how the
# run went: the last try is about an hour and a half after the first.
REPORT_RETRY = Retry(max=5, interval=[30, 120, 600, 1800, 3600])
REPORT_TIMEOUT = 10.0


def settled(run: PrintRun) -> None:
    """Queue the report to the WMS for a run it asked for. Queued rather
    than sent, and never raises, so a WMS that is down holds up no label."""
    if not run.wms_job_id or run.status not in SETTLED:
        return
    try:
        queue().enqueue(report_to_wms, run.id, retry=REPORT_RETRY)
    except Exception as exc:
        log.error("could not queue the report to the WMS: %s",
                  where(run=run.id, wms_job=run.wms_job_id, error=type(exc).__name__))


def report_to_wms(run_id: str) -> None:
    """Runs in the worker. Raises on a refusal so rq tries again."""
    from .settings import settings

    dbsession.engine()
    with dbsession.SessionLocal() as s:
        run = s.get(PrintRun, run_id)
        if run is None or not run.wms_job_id:
            return
        if not settings.wms_url:
            log.warning("no WMS_URL, so the WMS was not told: %s",
                        where(run=run_id, wms_job=run.wms_job_id, status=run.status))
            return
        if run.status == "done":
            body: dict[str, str] = {"status": "printed"}
        elif run.status == "cancelled":
            body = {"status": "failed", "message": "cancelled in Platen"}
        else:
            body = {"status": "failed",
                    "message": (run.error or "the run failed in Platen")[:500]}
        url = f"{settings.wms_url.rstrip('/')}/v1/print-jobs/{run.wms_job_id}/status"
        headers = {"Authorization": f"Bearer {settings.wms_key}"} if settings.wms_key else {}
        r = httpx.post(url, json=body, headers=headers, timeout=REPORT_TIMEOUT)
        if not 200 <= r.status_code < 300:
            log.warning("the WMS refused the report: %s",
                        where(run=run_id, wms_job=run.wms_job_id, http=r.status_code))
            raise RuntimeError(f"the WMS answered {r.status_code} to the report on {run_id}")
        log.info("WMS told: %s", where(run=run_id, wms_job=run.wms_job_id, status=body["status"]))
```

**Step 3:** Call `settled(run)` wherever a run reaches a settled state, after its `events.run_changed(run)`:
- `print_run`: the "printer no longer exists" return; the `cancelled` return inside the loop; the `except` block only when `not left` (before `raise`); after the final `events.run_changed(run)` at the end (status is `done`).
- `main.py` `cancel_run`, the `blocked` branch: `jobs.settled(run)` after `events.run_changed(run)`.

Not in `_queue_run`'s "could not queue" path: the WMS got a 503 there and sends again.

**Step 4:** `.venv/bin/pytest -q` → all pass.

---

### Task 6: Say so in Platen's README

**Files:** Modify `platen/README.md`

Add a short section after "Running it", headed "From Simple WMS". Cover these points:
- Make an operator key on the Keys screen, kept to the site if there is one.
- In the WMS, set the warehouse's `platen_url` to `https://<platen>/intake/wms` and `WMS_PLATEN_KEY` to that key.
- Name each Platen template after the WMS template (`carton-label`, `pick-list`, ...). Bind its fields to the shapes in `GET /v1/print-templates`.
- Name each printer after the WMS print point's printer, either its id or its name.
- In Platen, set `WMS_URL` and `WMS_KEY`, a WMS API key with `printing:write`, so the WMS hears `printed` or `failed`.
- A page template gets one row per entry in `lines`, with the header beside each row.

---

### Task 7: Simple WMS sends the key

**Files:** Modify `simple-wms/api/wms/config.py`, `simple-wms/api/wms/worker.py` (`send_print_jobs`), `simple-wms/docker-compose.yml` (worker env), `simple-wms/.env.example`, `simple-wms/docs/api.md` (Print jobs section); test in `simple-wms/api/tests/test_printing.py`

**Step 1: Failing test** (next to `test_worker_sends_a_job_to_platen_and_records_accepted`):

```python
def test_the_platen_key_rides_along_when_there_is_one(client, db, structure, headers, listener,
                                                     monkeypatch):
    from wms.worker import send_print_jobs

    set_platen(client, headers, listener)
    job = queue_one(client, db, structure, headers)
    monkeypatch.setenv("WMS_PLATEN_KEY", "platen_live_key")
    with httpx.Client() as http:
        send_print_jobs(db, http, now=job.next_attempt_at)
    assert listener.received[0]["headers"]["Authorization"] == "Bearer platen_live_key"
```

and in the existing accepted test add: `assert "Authorization" not in sent["headers"]`.

Run `cd simple-wms/api && .venv/bin/pytest tests/test_printing.py -q` (needs `docker compose up -d db`) → the new test fails.

**Step 2: Implement.** `config.py`, next to `worker_http_timeout_seconds`:

```python
    # Platen's operator key, sent as a bearer token with every print job.
    # Here and not in warehouse settings, which anyone who may read settings sees.
    platen_key: str = ""
```

`worker.py`, after `headers = {...}` in `send_print_jobs`:

```python
        if key := get_settings().platen_key:
            headers["Authorization"] = f"Bearer {key}"
```

`docker-compose.yml` worker `environment`: `WMS_PLATEN_KEY: ${WMS_PLATEN_KEY:-}`.

`.env.example`:

```
# Platen: an operator key made on Platen's Keys screen, sent with every print
# job. The address is the warehouse's platen_url setting:
# https://<platen>/intake/wms
# WMS_PLATEN_KEY=
```

`docs/api.md`, in "Print jobs (to Platen)" after the Headers line: "With `WMS_PLATEN_KEY` set, also `Authorization: Bearer <key>`. Against Platen the URL is `https://<platen>/intake/wms`, and Platen calls `POST /v1/print-jobs/{job_id}/status` back with a key that has `printing:write`."

**Step 3:** `.venv/bin/pytest tests/test_printing.py -q` → pass; then the full API suite `.venv/bin/pytest -q`.

---

### Task 8: Check it all

- `cd platen && .venv/bin/pytest -q && .venv/bin/ruff check app tests db`
- `cd simple-wms/api && .venv/bin/pytest -q`
- `git -C platen status` and `git -C simple-wms status`: only the files above changed. Report to the user and leave commits to them.
