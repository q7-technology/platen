# Platen

Label printing that binds Zebra templates straight to a database.

A row in your warehouse or ERP database becomes a label on the printer you
already own. Draw the template once in millimetres, bind its fields to a saved
query, and the people on the floor pick a template, answer a question or two,
and press one button.

MIT licensed. Built by [Q7 Technology](https://q7technology.com.au) in Ballarat.

## What it does

- **A canvas in millimetres.** Text, barcodes, QR, images and shapes at your
  printer's real dot pitch — 203, 300 or 600 dpi.
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

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload     # API
rq worker platen                  # worker, in a second terminal
```

Needs Python 3.11+, a Postgres for its own storage and a Redis for the queue.

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

Early. The storage layer is deliberately thin — `TEMPLATES`, `PRINTERS` and
`RUNS` are module-level dicts waiting to become real tables. Auth is left to
whatever you already use.

## Contributing

Issues and pull requests welcome, under the MIT licence. `CLAUDE.md` documents
the conventions and the handful of invariants that must not break quietly.

## Licence

[MIT](LICENSE).
