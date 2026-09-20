"""Timestamps leave the API with a time zone on them, on every database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.conftest import template_body

PARAMS = {"despatch_date": "2026-09-18"}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_a_template_edit_time_carries_its_time_zone(seeded):
    row = next(t for t in seeded.get("/templates").json() if t["id"] == "carton")

    edited = _parse(row["updated_at"])

    assert edited.tzinfo is not None, "a naive timestamp reads as local time in a browser"
    assert abs(edited - datetime.now(timezone.utc)) < timedelta(minutes=1)


def test_a_run_carries_its_time_zone_too(seeded):
    run_id = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                        "params": PARAMS}).json()["id"]

    created = _parse(seeded.get(f"/runs/{run_id}").json()["created_at"])

    assert created.tzinfo is not None
    assert abs(created - datetime.now(timezone.utc)) < timedelta(minutes=1)


def test_a_published_version_carries_its_time_zone(seeded):
    seeded.put("/templates/carton", json=template_body("blank"))
    published = _parse(seeded.post("/templates/carton/publish").json()["published_at"])

    assert published.tzinfo is not None
    assert abs(published - datetime.now(timezone.utc)) < timedelta(minutes=1)
