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
  models.py       template + element schema (pydantic), mm↔dots
  binding.py      "{{ order.sku }}" → a value from a row
  datasources.py  connection registry, read-only query runner
  images.py       base64 → PIL → 1-bit → ^GFA
  zpl.py          element tree → ^XA … ^XZ
  preview.py      the same tree → PNG, for the browser
  printers.py     raw 9100 / CUPS / local agent transports
  jobs.py         redis queue worker, retries, cancel
  main.py         the HTTP surface
db/             Platen's own storage: SQLAlchemy models, Alembic migrations
tests/          pytest; SQLite and fakeredis, no Docker needed
web/            the public landing page (static, single file)
  studio/         the operator screens, served by the API under /studio
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
   a fixed list of named filters. No `eval`, no expression parser, no
   attribute traversal into callables. Templates arrive from a browser.
2. **Queries are read-only and parameterised.** Connections use a read-only
   role; every query runs as a prepared statement. Never interpolate an
   operator's input into SQL.
3. **Every label in a run renders before the first one prints.** A render
   failure must surface as a message, not as half a roll of ruined stock.
4. **Cancel is checked between labels**, not between batches.
5. **A missing image degrades, it doesn't crash** — the element is skipped and
   a warning rides along with the run, unless `on_missing="fail"`.

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
