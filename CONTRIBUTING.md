# Contributing to Platen

Issues and pull requests are welcome, under the same MIT licence as the rest.

## Getting it running

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

The tests need neither Docker nor a database: they run against SQLite with a
fake Redis and a fake printer. If `pytest` passes on a clean checkout, your
environment is fine.

To see it work for real, `cp .env.example .env` and `docker compose up`.

## Before you open a pull request

```bash
ruff check app db tests agent
pytest
```

CI runs both of those, proves the migrations go up and down against Postgres
16 with no drift against the models, and brings up the compose stack to check
the documented install still works.

## What a change needs

**A test that failed before it.** Not for its own sake: most of the defects in
this repo's history were found by writing the test first or by driving a screen
in a browser, and the ones that got through were the paths nobody exercised.

**A migration, if it touches `db/models.py`.** `alembic revision --autogenerate`,
then read what it wrote. The test suite runs the same models on SQLite, so keep
column types portable and use `UtcDateTime` rather than a bare `DateTime`.

**Nothing that quietly breaks an invariant.** `CLAUDE.md` lists them. They are
short, and each one is there because getting it wrong ruins a roll of stock, or
prints a barcode twice, or hands somebody a credential.

## Style

Python 3.11+, `from __future__ import annotations` at the top of every module,
type hints on anything public. Dataclasses for plain records; pydantic only
where something crosses the HTTP boundary or is persisted as JSON.

Comments explain *why*. An error message names what went wrong and what to do
about it: `"consignment_barcode: the query returned no column 'consignment_no'"`,
not `"render failed"`.

Commit messages are a subject line in the imperative under 72 characters, a
blank line, and a body if it needs one. No trailers.

## The screens

`web/studio/` is plain HTML, one file per screen, sharing `studio.css` and
`studio.js`. There is no build step and no framework, and it would take a good
reason to add either. The design system is in `CLAUDE.md`: one dark theme, two
text values, two hues per screen, lucide icons.
