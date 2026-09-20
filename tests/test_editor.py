"""What the template editor needs from the API."""

from __future__ import annotations

from tests.conftest import template_body

PARAMS = {"despatch_date": "2026-09-18"}


def test_a_draft_previews_before_its_parameters_are_answered(seeded):
    """Opening the editor shouldn't demand a despatch date to draw the label."""
    r = seeded.post("/templates/carton/preview.png", json={"data": "columns"})

    assert r.status_code == 200, r.text
    assert r.content.startswith(b"\x89PNG")


def test_a_column_preview_stands_each_column_in_for_its_own_value(seeded):
    """Before anyone answers a parameter, the label should still show where
    each field lands — so a column prints its own name."""
    with_data = seeded.post("/templates/carton/preview.zpl", json={"params": PARAMS}).text
    columns = seeded.post("/templates/carton/preview.zpl", json={"data": "columns"}).text

    assert "Consignee 1" in with_data
    assert "Consignee 1" not in columns
    assert "consignee_name" in columns
    assert columns.startswith("^XA")


def test_a_column_preview_still_reports_a_binding_that_cannot_resolve(seeded):
    """An element bound to a column that isn't there is the editor's business
    to show, not something this mode should paper over."""
    body = template_body("fail")
    body["elements"][0]["value"] = "{{ nonexistent_column }}"
    seeded.put("/templates/carton", json=body)

    r = seeded.post("/templates/carton/preview.zpl", json={"data": "columns"})

    assert r.status_code == 422
    assert "consignee" in r.json()["detail"] and "nonexistent_column" in r.json()["detail"]


def test_a_template_with_print_history_is_not_deleted_silently(seeded):
    run = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                     "params": PARAMS}).json()["id"]

    r = seeded.delete("/templates/carton")

    assert r.status_code == 409
    assert run in r.json()["detail"]
    assert seeded.get("/templates/carton").status_code == 200


def test_an_unprinted_template_is_deleted_with_its_versions(seeded):
    seeded.put("/templates/scratch", json={**template_body("blank"), "id": "scratch"})
    seeded.post("/templates/scratch/publish")

    assert seeded.delete("/templates/scratch").status_code == 204
    assert seeded.get("/templates/scratch").status_code == 404
    assert seeded.get("/templates/scratch/versions").status_code == 404


def test_a_draft_keeps_its_published_version_number_in_the_list(seeded):
    draft = template_body("blank")
    draft["name"] = "Carton despatch"
    seeded.put("/templates/carton", json=draft)

    row = next(t for t in seeded.get("/templates").json() if t["id"] == "carton")
    assert row["name"] == "Carton despatch"
    assert row["version"] == 1                        # the draft edit didn't publish


def test_the_editor_screens_are_served(client):
    for path in ("/studio/templates", "/studio/editor"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("text/html")


def test_the_editor_can_list_a_querys_columns_without_its_parameters(seeded):
    body = seeded.get("/queries/cartons/columns").json()

    assert body["columns"] == ["consignment_no", "consignee_name", "despatch_date", "logo"]


def test_a_broken_query_says_so_rather_than_returning_no_columns(seeded):
    seeded.put("/queries/cartons", json={
        "datasource_id": "warehouse", "name": "Cartons", "sql": "select * from no_such_table"})

    r = seeded.get("/queries/cartons/columns")

    assert r.status_code == 422
    assert "no_such_table" in r.json()["detail"]


def _with_filter(seeded, expr: str) -> None:
    body = template_body("blank")
    body["elements"][0]["value"] = expr
    seeded.put("/templates/carton", json=body)


def test_a_filter_given_the_wrong_sort_of_value_names_the_problem(seeded):
    """`consignee_name | round:1` is a mistake, and the editor has to be told
    which element made it — not handed a 500."""
    _with_filter(seeded, "{{ consignee_name | round:1 }}")

    for endpoint in ("preview.zpl", "preview.png"):
        r = seeded.post(f"/templates/carton/{endpoint}", json={"params": PARAMS})
        assert r.status_code == 422, f"{endpoint} gave {r.status_code}"
        assert "consignee" in r.json()["detail"]
        assert "round" in r.json()["detail"]


def test_a_column_preview_leaves_filters_alone(seeded):
    """A column standing in for its own value can't be rounded or date-formatted,
    and saying so would report a fault the template doesn't have."""
    _with_filter(seeded, "{{ consignee_name | upper }} {{ despatch_date | date:%d/%m/%y }}")

    r = seeded.post("/templates/carton/preview.zpl", json={"data": "columns"})

    assert r.status_code == 200, r.text
    assert "consignee_name" in r.text and "despatch_date" in r.text


def test_an_unknown_filter_is_still_an_error_in_column_mode(seeded):
    _with_filter(seeded, "{{ consignee_name | shout }}")

    r = seeded.post("/templates/carton/preview.zpl", json={"data": "columns"})

    assert r.status_code == 422
    assert "shout" in r.json()["detail"]
