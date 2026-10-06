"""Sites: an operator at one, a manager across several, an administrator over
all of them — and a printer in no site shared by everyone."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app
from db import session as dbsession

PARAMS = {"despatch_date": "2026-09-18"}
PASSWORD = "a-long-enough-password"


def _sign_in(who: str) -> TestClient:
    c = TestClient(app)
    assert c.post("/auth/login", json={"username": who, "password": PASSWORD}).status_code == 200
    return c


def _printer(client: TestClient, printer_id: str, site_id: str | None) -> None:
    assert client.put(f"/printers/{printer_id}", json={
        "name": printer_id.title(), "dpi": 203, "transport": {"kind": "memory"},
        "site_id": site_id}).status_code == 200


@pytest.fixture
def two_sites(seeded) -> TestClient:
    """Ballarat and Geelong, a printer at each, and the seeded 'dock' left
    shared. Returns the administrator."""
    for site_id, name in (("bal", "Ballarat DC"), ("gee", "Geelong")):
        assert seeded.put(f"/sites/{site_id}", json={"name": name}).status_code == 200
    _printer(seeded, "bal-despatch", "bal")
    _printer(seeded, "gee-despatch", "gee")
    return seeded


def _person(admin: TestClient, username: str, role: str, site_ids: list[str]) -> TestClient:
    r = admin.put(f"/users/{username}", json={
        "name": username.title(), "role": role, "password": PASSWORD, "sites": site_ids})
    assert r.status_code == 200, r.text
    assert r.json()["sites"] == sorted(site_ids)
    return _sign_in(username)


def _printer_ids(c: TestClient) -> set[str]:
    return {p["id"] for p in c.get("/printers?probe=false").json()}


def _print(c: TestClient, printer_id: str):
    return c.post("/runs", json={"template_id": "carton", "printer_id": printer_id,
                                 "params": PARAMS})


def test_an_install_with_no_sites_works_as_it_did(operator):
    assert _printer_ids(operator) == {"dock"}
    assert _print(operator, "dock").status_code == 202
    assert operator.get("/auth/me").json()["sites"] == []


def test_an_operator_sees_their_site_and_the_shared_printers(two_sites):
    sam = _person(two_sites, "sam", "operator", ["bal"])
    assert _printer_ids(sam) == {"dock", "bal-despatch"}
    assert [s["id"] for s in sam.get("/sites").json()] == ["bal"]
    assert sam.get("/auth/me").json()["sites"] == ["bal"]


def test_an_operator_cannot_print_at_another_site(two_sites):
    sam = _person(two_sites, "sam", "operator", ["bal"])
    r = _print(sam, "gee-despatch")
    assert r.status_code == 403
    assert "Geelong" in r.json()["detail"]
    assert _print(sam, "bal-despatch").status_code == 202


def test_an_operator_works_at_one_site_only(two_sites):
    r = two_sites.put("/users/sam", json={"role": "operator", "password": PASSWORD,
                                          "sites": ["bal", "gee"]})
    assert r.status_code == 422
    assert "manager" in r.json()["detail"]
    # and nothing was half-made
    assert "sam" not in {u["username"] for u in two_sites.get("/users").json()}


def test_a_manager_looks_after_the_sites_they_were_given(two_sites):
    assert two_sites.put("/sites/hor", json={"name": "Horsham"}).status_code == 200
    _printer(two_sites, "hor-despatch", "hor")
    mia = _person(two_sites, "mia", "manager", ["bal", "gee"])
    assert _printer_ids(mia) == {"dock", "bal-despatch", "gee-despatch"}
    assert _print(mia, "gee-despatch").status_code == 202
    assert _print(mia, "hor-despatch").status_code == 403


def test_runs_at_another_site_stay_out_of_sight(two_sites):
    there = _print(two_sites, "gee-despatch").json()["id"]
    sam = _person(two_sites, "sam", "operator", ["bal"])
    here = _print(sam, "bal-despatch").json()["id"]

    seen = {r["id"] for r in sam.get("/runs").json()}
    assert here in seen and there not in seen
    assert {r["id"] for r in sam.get("/dashboard").json()["runs"]} == {here}
    assert sam.get(f"/runs/{there}").status_code == 403
    assert sam.post(f"/runs/{there}/cancel").status_code == 403
    assert sam.post(f"/runs/{there}/retry").status_code == 403
    # the administrator still sees both
    assert {here, there} <= {r["id"] for r in two_sites.get("/runs").json()}


def test_the_dashboard_counts_only_your_sites(two_sites):
    _print(two_sites, "gee-despatch")
    sam = _person(two_sites, "sam", "operator", ["bal"])
    board = sam.get("/dashboard").json()
    assert board["printers"]["total"] == 2          # bal-despatch and the shared dock
    assert board["runs"] == []


def test_a_saved_run_pinned_to_another_site_is_hidden(two_sites):
    for saved_id, printer_id in (("bal-am", "bal-despatch"), ("gee-am", "gee-despatch"),
                                 ("any", None)):
        assert two_sites.put(f"/saved-runs/{saved_id}", json={
            "name": saved_id, "template_id": "carton", "printer_id": printer_id,
            "params": PARAMS}).status_code == 200
    sam = _person(two_sites, "sam", "operator", ["bal"])
    assert {r["id"] for r in sam.get("/saved-runs").json()} == {"bal-am", "any"}


def test_a_site_is_archived_not_deleted(two_sites):
    r = two_sites.post("/sites/bal/archive")
    assert r.status_code == 409
    assert "Bal-Despatch" in r.json()["detail"]
    _printer(two_sites, "bal-despatch", None)
    _person(two_sites, "sam", "operator", ["bal"])

    assert two_sites.post("/sites/bal/archive").json()["archived_at"]
    assert two_sites.delete("/sites/bal").status_code == 405     # there is no delete
    assert "bal" not in {s["id"] for s in two_sites.get("/sites").json()}
    assert "bal" in {s["id"] for s in two_sites.get("/sites?archived=true").json()}
    # who looked after it is kept, so a restore brings them back with it
    assert _user(two_sites, "sam")["sites"] == ["bal"]
    assert two_sites.post("/sites/bal/restore").json()["archived_at"] is None
    assert [s["id"] for s in _sign_in("sam").get("/sites").json()] == ["bal"]


def test_nothing_new_goes_into_an_archived_site(two_sites):
    _printer(two_sites, "bal-despatch", None)
    two_sites.post("/sites/bal/archive")
    r = two_sites.put("/printers/bal-despatch", json={
        "name": "B", "transport": {"kind": "memory"}, "site_id": "bal"})
    assert r.status_code == 422 and "archived" in r.json()["detail"]
    r = two_sites.put("/users/mia", json={"role": "manager", "password": PASSWORD,
                                          "sites": ["bal"]})
    assert r.status_code == 422 and "archived" in r.json()["detail"]
    assert two_sites.put("/sites/bal", json={"name": "Renamed"}).status_code == 409


def _user(admin: TestClient, username: str) -> dict:
    return next(u for u in admin.get("/users").json() if u["username"] == username)


def test_a_printer_cannot_go_in_a_site_that_isnt_there(two_sites):
    r = two_sites.put("/printers/x", json={"name": "X", "transport": {"kind": "memory"},
                                           "site_id": "nowhere"})
    assert r.status_code == 422


def test_only_an_administrator_makes_sites(two_sites):
    mia = _person(two_sites, "mia", "manager", ["bal"])
    assert mia.put("/sites/new", json={"name": "New"}).status_code == 403
    assert mia.post("/sites/gee/archive").status_code == 403
    assert mia.get("/users").status_code == 403


def test_an_administrator_needs_no_sites(two_sites):
    r = two_sites.put("/users/root", json={"role": "admin", "password": PASSWORD,
                                           "sites": ["bal"]})
    assert r.json()["sites"] == []
    assert _printer_ids(_sign_in("root")) == {"dock", "bal-despatch", "gee-despatch"}


def test_leaving_sites_out_keeps_them(two_sites):
    _person(two_sites, "mia", "manager", ["bal", "gee"])
    r = two_sites.put("/users/mia", json={"name": "Mia", "role": "manager"})
    assert r.json()["sites"] == ["bal", "gee"]
    # but demoting a manager with two sites needs telling which one stays
    r = two_sites.put("/users/mia", json={"name": "Mia", "role": "operator"})
    assert r.status_code == 422
    r = two_sites.put("/users/mia", json={"name": "Mia", "role": "operator", "sites": ["gee"]})
    assert r.json()["sites"] == ["gee"]


def test_deleting_someone_takes_their_sites_with_them(two_sites):
    _person(two_sites, "sam", "operator", ["bal"])
    assert two_sites.delete("/users/sam").status_code == 204
    with dbsession.SessionLocal() as s:
        from app import sites
        assert sites.assigned(s, "sam") == []


def test_a_key_sees_every_site(two_sites):
    token = two_sites.post("/keys", json={"name": "wms", "role": "operator"}).json()["token"]
    c = TestClient(app, headers={"authorization": f"Bearer {token}"})
    assert _printer_ids(c) == {"dock", "bal-despatch", "gee-despatch"}


def test_the_command_line_knows_about_managers():
    assert "manager" in auth.ROLES


def test_a_key_made_for_one_site_keeps_to_it(two_sites):
    made = two_sites.post("/keys", json={"name": "bal-wms", "role": "operator", "sites": ["bal"]})
    assert made.status_code == 201 and made.json()["sites"] == ["bal"]
    c = TestClient(app, headers={"authorization": f"Bearer {made.json()['token']}"})
    assert _printer_ids(c) == {"dock", "bal-despatch"}
    assert _print(c, "gee-despatch").status_code == 403
    assert _print(c, "bal-despatch").status_code == 202
    listed = {k["name"]: k for k in two_sites.get("/keys").json()}
    assert listed["bal-wms"]["sites"] == ["bal"]


def test_an_administrator_key_has_no_sites_to_keep_to(two_sites):
    r = two_sites.post("/keys", json={"name": "x", "role": "admin", "sites": ["bal"]})
    assert r.status_code == 422
    assert two_sites.post("/keys", json={"name": "y", "role": "operator",
                                         "sites": ["nowhere"]}).status_code == 422
