# Platen

Label printing that binds Zebra templates straight to a database.

A row in your warehouse or ERP database becomes a label on the printer you
already own. Draw the template once in millimetres, bind its fields to a saved
query, and the people on the floor pick a template, answer a question or two,
and press one button.

MIT licensed. Built by [Q7 Technology](https://q7technology.com.au) in Ballarat.

## What it does

- **A canvas in millimetres.** Text, barcodes, QR, images and shapes at your
  printer's real dot pitch — 203, 300 or 600 dpi. Already have ZPL? Paste it
  in and carry on editing; the importer says what it couldn't carry over.
- **Binds to what you already run.** Postgres, SQL Server, MySQL, SQLite or a
  REST endpoint. Read-only role, prepared statements, parameters the operator
  answers at print time.
- **The symbologies that matter.** Code 128 with automatic subsets, GS1-128,
  Code 39, Interleaved 2 of 5, QR and Data Matrix.
- **Images out of your data.** A base64 column — a compliance mark, a
  signature, a photo — is decoded, scaled to the printer's dots, dithered and
  sent as a `^GFA` graphic.
- **Printers however they're wired.** Raw TCP on 9100, a small agent for USB,
  or an existing CUPS queue. No drivers on anyone's laptop.
- **Jobs you can answer for.** Queued, retried, cancellable between labels, with
  warnings attached to the run.

## Running it

The whole stack, with its own Postgres and Redis:

```bash
cp .env.example .env
docker compose up --build
```

The API is on http://localhost:8000 and runs the migrations before it starts.
The screens are at http://localhost:8000/studio/print (run a job),
`/studio/templates` (the library and the editor), `/studio/data` (connections
and saved queries) and `/studio/printers`. The API reference is at
http://localhost:8000/docs.

On your own machine instead:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg://platen:platen@localhost:5432/platen
export REDIS_URL=redis://localhost:6379/0
alembic upgrade head              # once, and after pulling a new migration
uvicorn app.main:app --reload     # API
rq worker platen                  # worker, in a second terminal
```

Needs Python 3.11+, a Postgres for its own storage and a Redis for the queue.
Every setting comes from the environment; `.env.example` lists them.

Tests need neither Docker nor a database:

```bash
pip install -r requirements-dev.txt
pytest
```

## How it fits together

```
browser → FastAPI → QueryRunner → BindingResolver → ZplRenderer → Redis → worker → socket → printer
                         ↑                               ↑
                  your database                    GfaEncoder (base64 → dots)
```

Every label in a run is rendered *before* the job is queued, so a broken
binding is a sentence on screen rather than half a roll of ruined stock. The
worker's only job is to open a socket and write.

`docs/design/` holds the interface artboards and `docs/reel.html` is a
one-minute walkthrough of the whole path, from the Print button to the print
head. Open either in a browser.

## Status

Early, but it goes end to end: draw a label, bind it to a query, and print it
without touching the API. Templates, data sources, printers and runs live in
Postgres (see `db/models.py`); publishing a template writes an immutable
version and every print run records which version it rendered from.

Not built yet: a job history screen and a printer network scan. Auth is left
to whatever you already use.

## Contributing

Issues and pull requests welcome, under the MIT licence. `CLAUDE.md` documents
the conventions and the handful of invariants that must not break quietly.

## Licence

[MIT](LICENSE).
