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
- **Binds to what you already run.** Postgres, MySQL, SQLite or a REST
  endpoint returning JSON, all of them proved in CI against a real server.
  SQL Server should work and nothing has confirmed it. Prepared statements
  under a read-only role for a database, GET and nothing else for an endpoint,
  and parameters the operator answers at print time.
- **The symbologies that matter.** Code 128 with automatic subsets, GS1-128,
  Code 39, Interleaved 2 of 5, QR and Data Matrix.
- **Images out of your data.** A compliance mark, a signature, a photo — from
  a `bytea` column directly or a base64 one, whichever your query already
  returns — is decoded, scaled to the printer's dots, dithered and sent as a
  `^GFA` graphic.
- **Printers however they're wired.** Raw TCP on 9100, an existing CUPS queue,
  or a small agent for a printer plugged into somebody's machine. No drivers on
  anyone's laptop. Platen can find the ones already on your network, private
  ranges only.
- **Jobs you can answer for.** Keep a run you do every morning and load it
  back. Hold the whole queue and let it go again; a run that was printing stops
  between labels and resumes where it stopped. Wait after each label when
  somebody is feeding the stock by hand. Choose which records go on the roll, put a
  separator in front of the job, take the whole run as a PDF before you commit
  stock to it. Queued, cancellable between labels, and retried twice by the
  worker on its own. A retry resumes rather than restarts, so a
  label that already came out is never printed twice. Warnings stay attached
  to the run.

## Running it

The whole stack, with its own Postgres and Redis:

```bash
cp .env.example .env
docker compose up --build
```

The API is on http://localhost:8000 and runs the migrations before it starts.
The screens are at http://localhost:8000/studio/dashboard (what is going on),
`/studio/print` (run a job),
`/studio/templates` (the library and the editor), `/studio/data` (connections
and saved queries), `/studio/printers` and `/studio/jobs`. The API reference is
at http://localhost:8000/docs. The landing page is at http://localhost:8080.

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


## Looking after it

Platen holds your templates, so its own database is worth backing up. For the
compose stack that is the `pgdata` volume:

```bash
docker compose exec -T db pg_dump -U platen platen | gzip > platen-$(date +%F).sql.gz
```

Migrations run when the API starts, so an upgrade is `docker compose pull &&
docker compose up -d`. Take the dump first: migrations go forward on their own
and back only by hand.

**It says what it is doing.** The API and the worker log to stderr, so compose
and journalctl pick them up without any setup:

```
platen.jobs  run started: run=JOB-2DFF05 printer=despatch printed=0 total=12 attempt=1
platen.jobs  run done: run=JOB-2DFF05 printer=despatch printed=12
platen.auth  sign-in refused: user=dave known=True
```

Refused sign-ins, lockouts, failed queries and every run that starts, finishes,
fails, is held or is waiting on somebody. `PLATEN_LOG_LEVEL=debug` for more.
No password, key or connection string is ever written to it, so the log is one
you can ship off the box.

**Things are thrown away on a schedule**, because they would otherwise not be.
A five thousand label run writes about twenty megabytes of ZPL, and nobody
reads last April's. The defaults:

| What | Kept for |
| --- | --- |
| The ZPL of a finished run | 14 days |
| The run itself, its counts and warnings | 365 days |
| The audit log | 365 days |
| Labels an agent has already printed | 7 days |

The labels go first and the history stays, so job history still tells you what
happened long after the ZPL is gone. Nothing unfinished is ever touched,
however old it looks. Change the windows under **Settings** on the dashboard,
or set one to zero to keep it forever. The API prunes once a day by itself;
`python -m app.prune --dry-run` says what would go.

**It says how the printers are.** Every 30 seconds the API asks each socket
printer `~HQES`, the Zebra's own report on itself, and keeps the answer: out
of labels, head open, out of ribbon, labels nearly out, and the rest. The
Printers screen shows it. A CUPS queue or an agent can only say whether it
answered. Each API process runs its own check, so two of them ask twice.

**The screens update themselves.** The worker and the API announce every
change on a Redis channel: a run queued, each label as it comes out, a run
done, a printer's health changing, the queue held. `GET /events` passes them
on to the browser as server-sent events, and only the ones about printers at
the viewer's own sites. They are nudges, not records: a screen that misses
one catches up on its next read, and a Redis that has gone away never stops
a label from printing. Behind nginx, turn buffering off for `/events`
(`proxy_buffering off;`) or nothing arrives until the stream closes.

## Who can do what

Three roles. An **administrator** wires up connections, printers and templates.
An **operator** picks a template, answers the question it asks and presses
print — and is never shown a connection string or the SQL behind their query.
A **manager** does what an operator does, across more than one site.

**Sites** are the buildings your printers are in. Make them under
**Printers → Sites**, put each printer in one, and tick who looks after which
under **People**. An operator works at one site, a manager at any number, and
an administrator sees them all. People only see the printers and runs at
their own sites. A printer in no site is shared with everyone, which is how
an install that has never made a site carries on exactly as it did. A key
sees every site, as an administrator would.

The first administrator is made once at startup from `PLATEN_ADMIN_USERNAME`
and `PLATEN_ADMIN_PASSWORD`. After that, **People** is where you add someone,
change a role, reset a password, switch an account off or delete it. There is
also a command, for when nobody can get in:

```bash
python -m app.adduser dave --role operator     # or manager, or admin
```

Resetting a password signs that person out everywhere, and so does switching
them off. A reset you do because a password leaked is no use if the session
somebody already has keeps working.

With no users and nothing in the environment, nobody can sign in. That is the
safe way round; the log says how to fix it.

Passwords are hashed with scrypt at about 64 MB a go. A session is a random
token in an http-only, same-site cookie, and only its hash is stored, so a
stolen backup cannot be replayed as a login. The cookie is marked `Secure`
when the request arrives over https; set `PLATEN_SECURE_COOKIES=true` if a
proxy terminates TLS without passing the scheme through.

Something other than a person signs in with a key instead of a password. Make
one under **People → Keys**, send it as `Authorization: Bearer <key>`, and
revoke it there when the script that used it is gone. A key is shown once,
when it is made, and only its hash is kept. An agent's key is narrower still:
it can talk to its own agent and nothing else.

## Pages as well as labels

Packing slips, invoices and pick lists go to an ordinary office printer as a
PDF. Make one under **Templates → New template → A page**. A page template is
a header, a body and a footer, each a list of blocks: text, a table, an image,
a barcode or QR code, a gap, a line. The header and footer are on every page
and can say `Page {{ page }} of {{ pages }}`.

One query, one document per group. A packing slip's query returns a row per
order line with the order's own columns alongside; set **One document per**
to `order_no` and each order gets its own slip, its lines in the table and
its first row behind everything else. A table grows with the data and carries
onto the next page with its headings repeated. The preview is a picture of
the very PDF that prints. Like a label, every document is made before the
first one prints, and a broken binding names the block and the column.

Add the office printer under **Printers** with **Prints: Pages (PDF)**. Most
office printers take a PDF straight down port 9100; for one that doesn't, use
its CUPS queue and CUPS converts it. A desk agent carries labels only for
now. An office printer is only ever asked whether it answers: `~HQES`, the
Zebra health question, would come out as a printed page. The network scan
asks `~HI` of everything on 9100, so an office printer in the range may print
one short page when you scan.

Labels go to label printers and pages to office printers, and a move or a
detour keeps to its own kind.

## The live map

`/studio/map` is every site, printer and job at once, drawn as a small model
town at night. Sites are buildings on a map, with a lit window for each job
on and a gold glow when a printer there needs a look. Open one to see its
printers on a floor plan, with their jobs waiting on a belt in front of them
as white parcels. Drag a parcel onto another printer to move what's left of
it; the printers that would take it light up, and Undo takes it back.
Shift-drag across the floor, or shift-click, to pick up several jobs and move
them together. A job that moves to another site flies across the map. Click
a printer for its health and its queue, and, for a manager, a detour. Managers
can also drag printers into place under **Arrange printers**, and everyone at
the site sees the same layout. An operator starts inside their own site.

It updates itself from `/events`, so a label coming out moves the count on
the printer within a second. Every parcel and printer can be reached with the
keyboard and moved from the side panel, without dragging.

## When a printer stops

**Move a run.** Open it under **Job history** and pick another printer. What
has already come out stays out: only labels with nothing recorded against
them move. A run that is printing finishes the label under the head and
carries on at the new printer. One that is waiting on someone feeding stock
asks first, because the next label will come out somewhere else. The labels
were rendered at the template's dot pitch, so a run only moves to a printer
at the same dpi. Undo is moving it back. `POST /runs/{id}/move`.

**Detour a printer.** On **Printers**, send everything for a broken printer
to another one until you clear it. Its unfinished runs move straight away,
except a hand-fed one somebody is standing at, and new runs follow the
detour. Detours don't chain. Managers and administrators can set them, and
anyone can only move work between printers at their own sites.

## Printers on somebody's desk

A printer on a workstation's USB port needs an agent, because Platen cannot
open a socket to it. Register the workstation under **Printers → Agents**,
copy the key, and run this on that machine:

```bash
python platen_agent.py --server https://platen.example \
    --agent wks-office-02 --token plt_... --cups zd621-office
```

`--device /dev/usb/lp0` works instead of `--cups` where there is no print
server. The agent asks Platen for labels rather than Platen reaching in, so
nothing has to be open inbound to that machine. It is one file and needs
nothing but a Python interpreter.

A label is only counted as printed once the agent says it came out, the same
as a socket printer taking the bytes. If the agent is not running, the run
fails after a minute and the retry resumes it.

## Contributing

Issues and pull requests welcome, under the MIT licence. `CLAUDE.md` documents
the conventions and the handful of invariants that must not break quietly.

## Licence

[MIT](LICENSE).
