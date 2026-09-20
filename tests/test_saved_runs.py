"""A run somebody does every morning, kept so they do not set it up again."""

from __future__ import annotations

PARAMS = {"despatch_date": "2026-09-18"}


def _save(client, name: str = "Morning despatch", **extra) -> dict:
    body = {"name": name, "template_id": "carton", "printer_id": "dock",
            "params": PARAMS, "copies": 1, "separator": False, **extra}
    r = client.put(f"/saved-runs/{name.lower().replace(' ', '_')}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_run_can_be_kept_and_found_again(seeded):
    _save(seeded)

    rows = seeded.get("/saved-runs").json()

    assert [r["id"] for r in rows] == ["morning_despatch"]
    assert rows[0]["name"] == "Morning despatch"
    assert rows[0]["template_id"] == "carton"
    assert rows[0]["params"] == PARAMS


def test_what_was_kept_is_enough_to_print_from(seeded):
    saved = _save(seeded, copies=2, separator=True)

    run = seeded.post("/runs", json={
        "template_id": saved["template_id"], "printer_id": saved["printer_id"],
        "params": saved["params"], "copies": saved["copies"],
        "separator": saved["separator"]}).json()

    assert run["labels"] == 25          # 12 records, twice, and a separator


def test_saving_it_again_replaces_it(seeded):
    _save(seeded)
    _save(seeded, copies=4)

    rows = seeded.get("/saved-runs").json()

    assert len(rows) == 1
    assert rows[0]["copies"] == 4


def test_one_can_be_thrown_away(seeded):
    _save(seeded)

    assert seeded.delete("/saved-runs/morning_despatch").status_code == 204
    assert seeded.get("/saved-runs").json() == []


def test_it_will_not_keep_a_run_for_a_template_that_is_not_there(seeded):
    r = seeded.put("/saved-runs/nope", json={
        "name": "Nope", "template_id": "ghost", "printer_id": "dock", "params": {}})

    assert r.status_code == 422
    assert "ghost" in r.json()["detail"]


def test_an_operator_may_keep_and_use_one(operator):
    _save(operator, "Nights")

    assert [r["id"] for r in operator.get("/saved-runs").json()] == ["nights"]
