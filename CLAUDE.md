# Platen

Label printing that binds Zebra templates straight to a database. A FastAPI
service renders ZPL from a template plus a row of data and writes it at a
printer. MIT licensed, built by Q7 Technology in Ballarat.

## Git

- **Never add co-authors to commits.** No `Co-Authored-By:` lines, no
  `Generated with` trailers, no tool attribution of any kind. A commit message
  is a subject line, a blank line, and a body if it needs one.
- Subject in the imperative, under 72 characters: `Fix GFA row padding on odd widths`.
- Branch off `main`; never commit straight to it.
- Don't commit unless asked.

## Layout

```
app/            the service
  settings.py     everything read from the environment, in one place
  retention.py    what gets thrown away, and when
  logs.py         one place that decides what Platen says about itself
  auth.py         users, roles, password hashing, sessions
  sites.py        which sites somebody looks after, so which printers and runs they see
  models.py       template + element schema (pydantic), mm↔dots
  binding.py      "{{ order.sku }}" → a value from a row
  datasources.py  connections (SQL or REST), read-only query runner
  images.py       base64 → PIL → 1-bit → ^GFA
  zpl.py          element tree → ^XA … ^XZ
  pages.py        page templates (header, body, footer) → one PDF per document, via ReportLab
  zplimport.py    ^XA … ^XZ → element tree, with a list of what it dropped
  preview.py      the same tree → PNG, for the browser
  printers.py     raw 9100 / CUPS / local agent transports, and network discovery
  jobs.py         redis queue worker, retries, cancel
  events.py       live updates: publish on Redis, relay to browsers as server-sent events
  health.py       asks each printer how it is (~HQES) on a timer, keeps the answer
  main.py         the HTTP surface
agent/          the print agent, for a USB printer on a workstation. Standard
                library only, on purpose: it installs on a warehouse PC
db/             Platen's own storage: SQLAlchemy models, Alembic migrations
tests/          pytest; SQLite and fakeredis, no Docker needed
web/            the public landing page (static, single file)
  studio/         the operator screens, served by the API under /studio
                  studio.css and studio.js are shared; one page per screen,
                  vanilla DOM, no build step. The editor auto-saves the draft
                  and asks the server to re-render — the canvas background is
                  a real preview, not a CSS impression of one
docs/           the design canvas artboards and the walkthrough reel
```

## Running it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload     # API
rq worker --with-scheduler platen platen-reports # worker, second terminal
```

Storage is Postgres through SQLAlchemy 2 (`db/models.py`) with Alembic
migrations in `db/migrations/`. Add a migration for every schema change; the
test suite runs the same models on SQLite, so keep column types portable.

## Invariants — do not break these quietly

1. **A template can never execute anything.** Bindings are dotted lookups plus
   a fixed list of named filters (`binding.FILTERS`). No `eval`, no expression
   parser, no attribute traversal into callables. Templates arrive from a
   browser. A filter handed the wrong sort of value raises `FilterError`
   naming the filter and the value — never an unhandled exception.
2. **Queries are read-only and parameterised.** SQL connections use a
   read-only role and every query runs as a prepared statement. A REST source
   only ever issues a GET, and an operator's answer is quoted into the path or
   sent as a query parameter. Never interpolate an operator's input into a
   statement or a URL.
3. **Every label in a run renders before the first one prints.** A render
   failure must surface as a message, not as half a roll of ruined stock.
4. **Cancel is checked between labels**, not between batches. So is the queue
   hold, for the same reason: stopping halfway through one would leave it
   under the print head. A run stopped by the hold is `paused`; one waiting on
   a person feeding stock is `waiting`. They are different kinds of stopped and
   letting the queue go must not run off with a hand-fed job.
5. **A missing image degrades, it doesn't crash** — the element is skipped and
   a warning rides along with the run, unless `on_missing="fail"`.
6. **A connection's password never reaches the browser.** Reads mask it; a URL
   saved back unchanged keeps the stored one.
7. **The preview is what the printer will do**, at the printer's dot pitch —
   including dithering, and barcodes drawn at their real module width rather
   than scaled to fit a box. `preview.render_png` and `zpl.render_run` fail
   the same way, with the same message, on the same input.
8. **Timestamps leave the API knowing their time zone.** Use `UtcDateTime`,
   never a bare `DateTime` — SQLite drops the offset and a browser then reads
   UTC as local.
9. **An import says what it dropped.** `zplimport` collects a warning for
   every command it doesn't carry over, and the screen shows them before the
   label is opened. A silent importer is worse than none: the gap only turns
   up on stock.
10. **A retry resumes, it never restarts.** The worker sends only labels with
    no `printed_at`. A second consignment barcode on a second carton is worse
    than a missing one, so nothing that has come out of the printer is ever
    sent again.
11. **Every route names who may call it.** A route carries
    `Depends(current_user)` or `Depends(admin)`, and a test walks the route
    table to prove it. Adding an endpoint without one fails the suite, which
    is the point: an auth check you forget is worse than none.
12. **An operator never sees a credential.** Not a connection string, not the
    SQL behind their query. `get_query` returns a smaller body for them.
13. **A label is printed when it has come out, not when it was handed on.**
    `RawTcp.send` returns when the printer took the bytes; `Agent.send` waits
    for the agent to say the same. If "printed" meant "queued" for one
    transport and "printed" for another, `printed / total` would be a lie on
    exactly the printers nobody is standing next to.
14. **A key is shown once.** Only its hash is stored, like a session's. An
    agent key may reach its own agent's endpoints and nothing else.
15. **Taking access away works immediately.** A password reset, a switch-off
    and a delete all end that person's sessions. An administrator can never
    remove their own rights, and the last one cannot be removed at all — not
    even by an admin key, which isn't a person and so slips past the
    don't-delete-yourself rule.
16. **Retention throws the labels away, not the story.** A finished run loses
    its `run_label` rows first and the run, its counts and its warnings much
    later, so job history still answers "what happened" long after the ZPL is
    gone. A run that has not settled is never pruned, however old it looks.
17. **The log never carries a secret.** No password, key, token or connection
    string, so a site can ship it somewhere without thinking about it. Log the
    identifiers — run, printer, query, user — and the reason.
18. **Discovery stays on the site's own network.** `printers.scan` refuses
    anything but a private, loopback or link-local range, caps a call at 1024
    addresses, and opens one connection per address on the one port it was
    given. It is how you find your own printers, not a port sweep.

19. **Every route that touches a printer or a run checks the site.** An
    operator or manager sees printers in their own sites and printers in no
    site, and nothing else turns up in a list, a count or a dashboard. Asked
    for one by name, the answer is a 403 that says which site it is at, so
    they know who to ask. Use `sites.printer_filter` for a query and
    `_may_use` for a single printer; a run is checked through its printer.

20. **An event is a nudge, never the truth.** Publishing never raises: a
    Redis that has gone away must not stop a label from printing. Anything a
    screen shows comes from a read, and an event only says when to read
    again. `/events` relays only events about printers the viewer may see.

21. **A move never sends a label twice.** Moving a run changes where the
    labels with no `printed_at` go, and nothing else. A printing run is moved
    by the worker between labels, through `move_to`, like cancel; any other
    run is moved with an update that only lands if its status hasn't changed
    since it was read. A run only moves to a printer at its template's dpi,
    because its labels are already rendered. The worker claims a run
    (`queued` or `retrying` → `printing`) before it sends anything, so a late
    retry of a run that has moved on, or is printing elsewhere, does nothing.
    Moving a printing run back to the printer it is still on calls off the
    move it was waiting to make; that is what undo means there.

22. **A page is never markup and never sent ZPL.** Every bound value is
    escaped before it reaches a ReportLab paragraph, which would otherwise
    read tags (`<img src>` included) out of the data. The page preview is a
    picture of the PDF that prints. An office printer is probed, never asked
    `~HQES` or sent a test label: on port 9100 either comes out as a page.

23. **A run waits for a printer that says it can't print.** The worker checks
    the printer's last health answer between labels, like cancel, and stops
    as `blocked` on a fresh error. Only `health.resume_blocked` puts it back,
    and only once the printer answers ready or warning. An answer older than
    `HEALTH_FRESH` holds nothing: a stopped checker mustn't stop printing.

## Code

Python 3.11+. `from __future__ import annotations` at the top of every module.
Type hints on anything public. Dataclasses for plain records, pydantic only
where something crosses the HTTP boundary or is persisted as JSON.

Comments explain *why*, and only where the reason isn't obvious from the code.
No comment that restates the line under it.

Errors name what went wrong and what to do: `"consignment_barcode: the query
returned no column 'consignment_no'"`, not `"render failed"`.

## Design system

The studio screens have Platen's own look, **plum and lime**: a bright toy
town in daylight. The tokens live at the top of `web/studio/studio.css`; use
them rather than writing a colour into a page. The short version:

- **One theme, light.** Ground is warm paper `#fbf3ec`, cards are white with a
  `#eadccf` hairline and a soft shadow. There is no dark palette yet — don't
  add a `prefers-color-scheme` block piecemeal; a night mode is a whole set of
  tokens or nothing.
- **Two text values**: `--ink` `#2a2035` for headings and values, `--mut`
  `#6e6178` for prose, meta and captions.
- **Plum is the brand** (`--accent` `#7a4aa8`, `--accent-deep` `#5b3384` on
  hover): primary buttons, the current nav item, links, buildings on the map.
  White text on plum.
- **Lime means going well** (`--lime` `#9ccf3b`): progress bars, lit windows,
  a printer's lamp while it prints, the drop target. Lime is a fill, never
  text on white — use `--lime-text` `#4f7a12` for words.
- **Red is kept for one job: a person needs to look** (`--signal` `#c9343d`
  for text, `--signal-bright` `#e5484d` for pins, lamps and bars). Nothing
  decorative is red, so a red thing on the map is always a reason to walk
  over. The CSS still calls these classes `gold`; the name is historical.
- Parcels are cardboard brown, label stock is white with dark ink — a label is
  paper.
- **System font stack**, sans and mono. Don't add a webfont.
- **Icons**: lucide at 1.5px stroke, nothing else. Decorative ones get
  `aria-hidden="true"`; icon-only controls get an `aria-label`.
- **The mark is `web/platen-mark.svg`**, a plum box with a lime label. It sits
  next to the PLATEN wordmark, so its alt text is empty. The studio and the
  landing page (`web/index.html`) share the same tokens. Q7 Technology is
  credited in words; its logo isn't used, since it never goes on a light
  background.

Voice: plain Australian English. `I.T.`, never `Information Technology`. Claims
carry a number or a mechanism or they get cut. Sentence case everywhere except
uppercase eyebrows. Anything mocked or simulated says so, plainly, before
someone touches it.

## Licence

MIT. Contributions are accepted under the same licence. Keep the header in
`LICENSE` intact and don't add a second licence to a subdirectory.
