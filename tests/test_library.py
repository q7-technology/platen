"""Finding a template when there are more than a handful."""

from __future__ import annotations

from tests.conftest import template_body


def _make(client, id: str, name: str, folder: str = "") -> None:
    body = {**template_body("blank"), "id": id, "name": name, "folder": folder}
    assert client.put(f"/templates/{id}", json=body).status_code == 200


def test_a_template_remembers_which_folder_it_is_in(seeded):
    _make(seeded, "pallet", "Pallet SSCC", folder="Shipping")

    assert seeded.get("/templates/pallet").json()["folder"] == "Shipping"


def test_searching_matches_the_name_and_the_id(seeded):
    _make(seeded, "pallet", "Pallet SSCC", folder="Shipping")
    _make(seeded, "shelf_tag", "Retail shelf tag", folder="Product")

    assert [t["id"] for t in seeded.get("/templates", params={"q": "pallet"}).json()] == ["pallet"]
    assert [t["id"] for t in seeded.get("/templates", params={"q": "SHELF"}).json()] == ["shelf_tag"]
    assert [t["id"] for t in seeded.get("/templates", params={"q": "sscc"}).json()] == ["pallet"]


def test_searching_for_nothing_gives_everything(seeded):
    _make(seeded, "pallet", "Pallet SSCC")

    assert len(seeded.get("/templates", params={"q": ""}).json()) == 2


def test_a_folder_narrows_the_library(seeded):
    _make(seeded, "pallet", "Pallet SSCC", folder="Shipping")
    _make(seeded, "carcass", "Carcass tag", folder="Product")

    shipping = seeded.get("/templates", params={"folder": "Shipping"}).json()

    assert [t["id"] for t in shipping] == ["pallet"]


def test_the_folders_that_exist_are_listed_with_their_counts(seeded):
    _make(seeded, "pallet", "Pallet SSCC", folder="Shipping")
    _make(seeded, "courier", "Courier consignment", folder="Shipping")
    _make(seeded, "carcass", "Carcass tag", folder="Product")

    body = seeded.get("/templates/folders").json()

    assert body["total"] == 4
    assert body["folders"] == [{"name": "Product", "count": 1}, {"name": "Shipping", "count": 2}]
    assert body["unfiled"] == 1


def test_a_template_with_no_folder_is_unfiled_not_broken(seeded):
    assert seeded.get("/templates/carton").json()["folder"] == ""
