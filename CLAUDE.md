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
  auth.py         users, roles, password hashing, sessions
  models.py       template + element schema (pydantic), mm↔dots
  binding.py      "{{ order.sku }}" → a value from a row
  datasources.py  connections (SQL or REST), read-only query runner
  images.py       base64 → PIL → 1-bit → ^GFA
  zpl.py          element tree → ^XA … ^XZ
  zplimport.py    ^XA … ^XZ → element tree, with a list of what it dropped
  preview.py      the same tree → PNG, for the browser
  printers.py     raw 9100 / CUPS / local agent transports, and network discovery
  jobs.py         redis queue worker, retries, cancel
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
rq worker platen                  # worker, second terminal
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
4. **Cancel is checked between labels**, not between batches.
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
16. **Discovery stays on the site's own network.** `printers.scan` refuses
    anything but a private, loopback or link-local range, caps a call at 1024
    addresses, and opens one connection per address on the one port it was
    given. It is how you find your own printers, not a port sweep.

## Code

Python 3.11+. `from __future__ import annotations` at the top of every module.
Type hints on anything public. Dataclasses for plain records, pydantic only
where something crosses the HTTP boundary or is persisted as JSON.

Comments explain *why*, and only where the reason isn't obvious from the code.
No comment that restates the line under it.

Errors name what went wrong and what to do: `"consignment_barcode: the query
returned no column 'consignment_no'"`, not `"render failed"`.

## Design system

The UI and the public pages follow the **Q7 Technology** design system. Its
tokens live in the design-system artifact; the short version:

- **One theme, dark.** Ground is `#0d1117`. There is no light palette — never
  write `dark:` variants or a `prefers-color-scheme` block.
- **Surfaces are the ground again at 60% alpha** over the star field, with a
  blue hairline. Do not introduce lighter grey surfaces; depth comes from alpha
  and borders.
- **Two text values only**: `#ccd6f6` for headings and values, `#8892b0` for
  prose, meta and captions.
- **Two hues per screen, maximum.** `#29abe2` is the brand blue (accent word in
  a heading, icons, primary CTA, and borders at 10–30% alpha). `#f7941d` is the
  second voice — taglines, outline CTAs, and every "this is simulated" marker.
- **Light fills take dark text.** Blue, gold and mint fills get `#0d1117` on
  them, never white.
- **System font stack**, sans and mono. Don't add a webfont.
- **Icons**: lucide at 1.5px stroke, nothing else. Decorative ones get
  `aria-hidden="true"`; icon-only controls get an `aria-label`.
- **The logo is `q7-logo-128.png`.** Never redraw it, recolour it, or place it
  on a light background. Alt text is `Q7Technology Logo`.
- Label stock stays white with dark ink, in every theme — a label is paper.

Voice: plain Australian English. `I.T.`, never `Information Technology`. Claims
carry a number or a mechanism or they get cut. Sentence case everywhere except
uppercase eyebrows. Anything mocked or simulated says so, in gold, before
someone touches it.

## Licence

MIT. Contributions are accepted under the same licence. Keep the header in
`LICENSE` intact and don't add a second licence to a subdirectory.
