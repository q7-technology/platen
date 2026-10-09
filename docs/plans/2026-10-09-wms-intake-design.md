# Print jobs from Simple WMS

Simple WMS renders nothing. Its worker POSTs a template name, a version, a
printer, copies and JSON to the warehouse's `platen_url`, expects a 2xx to mark
the job `accepted`, and waits to be told `printed` or `failed` at
`POST /v1/print-jobs/{job_id}/status`. Platen's `/runs` can't take that body:
it wants ids, pulls its rows through a saved query, and reports to nobody.
This adds the missing door.

## The request

`POST /intake/wms`, `Depends(current_user)`, called with an operator key made
on the Keys screen. A key kept to some sites only reaches their printers
(invariant 19).

```json
{ "job_id": "uuid", "template": "carton-label", "version": "v3",
  "printer": "packing-bench-2", "copies": 1,
  "reference": { "type": "delivery", "ref": "0080012345" }, "data": { ... } }
```

- **Template**: the Platen template whose id is the WMS `template`, latest
  published version. Its saved query is not run; the rows come from `data`.
  The WMS version is recorded on the run's audit row, not used for lookup.
- **Printer**: by Platen printer id, then by name ignoring case, so
  `packing-bench-2` and `Packing bench 2` both land. A detour is followed, as
  `/runs` follows it.
- **Rows**: a label template gets one row, `data`. A page template gets one
  row per entry in `data.lines`, each carrying the header fields beside the
  line's own, so a table lists the lines and the heading binds
  `delivery_ref`. No `lines`, or an empty list, gives the one row `data`.
- **Copies** as sent.
- **Once only**: a `job_id` already seen returns the run it made and prints
  nothing. The WMS retries on a timeout, and a second carton label is worse
  than a late one.

Every label renders before the run is written (invariant 3), through the same
code `/runs` uses.

## The answer

| What happened | Status | The WMS then |
| --- | --- | --- |
| Queued | `202 {id, labels, warnings, duplicate}` | marks the job `accepted` |
| No such template, no published version, no such printer, a render error, wrong printer kind | `422` naming the field | retries on its backoff, then `failed` with the message |
| Printer at a site the key doesn't reach | `403` | same |
| Redis gone | `503` | retries |

## Storage

One migration: `print_run.wms_job_id`, a nullable string with a unique index.
It finds a repeat and it says the run is owed a callback.

## Telling the WMS

When a run with a `wms_job_id` settles, `jobs.py` queues
`report_to_wms(run_id)` on its own RQ queue, `platen-reports`, with
`Retry(max=5)` and a widening interval. The worker is started with
`--with-scheduler` (rq runs a retry with an interval only then) and drains
`platen` before `platen-reports`, so a WMS that is down never holds up a
label. A report is tried again only on a 401, 408, 429, a 5xx or no answer at
all; any other refusal (404, 422) is logged once and left.

| Run | Sent |
| --- | --- |
| `done` | `printed` |
| `failed` | `failed`, message `run.error` |
| `cancelled` | `failed`, message "cancelled in Platen" |

`POST {WMS_URL}/v1/print-jobs/{job_id}/status` with
`Authorization: Bearer {WMS_KEY}`, both read in `settings.py` and listed in
`.env.example`. No `WMS_URL`: the report is still queued, and the worker,
which reads its own environment, logs that it has nowhere to send it and
skips it. A `WMS_URL` with no `WMS_KEY` sends anyway, with a warning that the
WMS will likely refuse it. The key
never reaches the log (invariant 17). The WMS client behind the key needs
`printing:write`.

## Simple WMS

A `WMS_PLATEN_KEY` environment setting (`platen_key` under the settings'
`WMS_` prefix). When set, the worker adds
`Authorization: Bearer <key>` to every print job it sends. It lives in the
environment, not the warehouse settings, because those are returned to anyone
who may read settings. `docs/api.md` and `.env.example` say so.

## Tests

Platen (pytest, SQLite, fakeredis):
- a label job queues and its ZPL carries the values from `data`
- a page job expands `lines` into rows with the header beside each
- printer found by id and by name; detour followed
- a repeated `job_id` returns the same run and queues nothing
- unknown template, unpublished template, unknown printer: 422 with the name
- a key kept to another site: 403
- the route-table auth test still passes
- a run settling done, failed and cancelled queues the right callback; no
  `WMS_URL` sends none; the key is not in the captured log

Simple WMS (pytest):
- the worker sends the bearer header with `WMS_PLATEN_KEY` set and none without
