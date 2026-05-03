import csv
import io
import json

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.db as db
import app.main as main


class DummyUpload:
    def __init__(self, filename=None, content_type=None):
        self.filename = filename
        self.content_type = content_type


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    upload_dir = data_dir / "uploads"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "stock_tracker.db")
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "DB_PATH", data_dir / "stock_tracker.db")
    monkeypatch.setattr(main, "BSDATA_ROOT", data_dir / "bsdata")
    monkeypatch.setattr(main, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    upload_dir.mkdir(parents=True, exist_ok=True)

    with TestClient(main.app) as test_client:
        yield test_client


def _insert_catalogue_unit(
    *,
    name="Intercessor Squad",
    faction="Space Marines",
    wargear_options=None,
    model_composition=None,
):
    now = db.utc_now_sql()
    with db.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO bsd_units (
                game_system, bs_id, name, faction, catalogue_file, entry_type,
                points, min_models, max_models, keywords, stats_json,
                wargear_options_json, model_composition_json, active, imported_at
            ) VALUES (
                'wh40k_10e', 'unit-1', ?, ?, 'Space Marines.cat', 'unit',
                90, 5, 10, 'Infantry, Battleline', ?,
                ?, ?, 1, ?
            )
            """,
            (
                name,
                faction,
                json.dumps({"T": "4", "SV": "3+"}),
                json.dumps(wargear_options or []),
                json.dumps(model_composition or []),
                now,
            ),
        )
        return cursor.lastrowid


def test_status_game_systems_units_and_factions(client):
    _insert_catalogue_unit(name="Chaos Lords", faction="Heretic Astartes")

    status = client.get("/api/status").json()
    assert status["game_system"] == "wh40k_10e"
    assert status["unit_count"] == 1
    assert status["inventory_count"] == 0

    systems = client.get("/api/game-systems").json()
    assert {system["id"] for system in systems} >= {"wh40k_10e", "kill_team", "age_of_sigmar_4e"}

    factions = client.get("/api/factions").json()
    assert factions == [{"faction": "Heretic Astartes", "unit_count": 1}]

    units = client.get("/api/units", params={"query": "chaos lord"}).json()
    assert len(units) == 1
    assert units[0]["name"] == "Chaos Lords"
    assert units[0]["stats"] == {"T": "4", "SV": "3+"}
    assert units[0]["wargear_option_count"] == 0


def test_inventory_lifecycle_copy_progress_export_and_delete(client):
    create_response = client.post(
        "/api/inventory",
        json={
            "unit_name": "Custom Scouts",
            "faction": "Space Marines",
            "quantity": 2,
            "models_owned": 10,
            "built_count": 6,
            "painted_count": 4,
            "wargear": "Boltguns",
            "model_number": "SM-001",
            "storage_location": "Shelf A",
            "notes": " Ready for basing\n",
            "acquired_on": "2026-05-03",
        },
    )
    assert create_response.status_code == 201
    item = create_response.json()
    assert item["unit_name"] == "Custom Scouts"
    assert item["unbuilt_count"] == 4
    assert item["unpainted_count"] == 6
    assert [copy["copy_number"] for copy in item["copies"]] == [1, 2]
    assert [copy["built_count"] for copy in item["copies"]] == [6, 0]

    copy_id = item["copies"][1]["id"]
    copy_response = client.put(
        f"/api/inventory/{item['id']}/copies/{copy_id}",
        json={
            "models_owned": 5,
            "built_count": 2,
            "painted_count": 1,
            "model_number": "SM-001-B",
            "wargear": "Shotguns",
            "storage_location": "Shelf B",
            "notes": "second copy",
        },
    )
    assert copy_response.status_code == 200
    assert copy_response.json()["built_count"] == 2

    inventory = client.get("/api/inventory").json()
    assert len(inventory) == 1
    assert inventory[0]["built_count"] == 8
    assert inventory[0]["painted_count"] == 5

    export_response = client.get("/api/export.csv")
    assert export_response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(export_response.text)))
    assert rows[0]["unit_name"] == "Custom Scouts"
    assert rows[0]["built_count"] == "8"
    assert rows[0]["painted_count"] == "5"

    delete_response = client.delete(f"/api/inventory/{item['id']}")
    assert delete_response.status_code == 204
    assert client.get("/api/inventory").json() == []


def test_inventory_from_catalogue_unit_uses_wargear_selection_summary(client):
    unit_id = _insert_catalogue_unit(
        wargear_options=[
            {"key": "bolt-rifle", "name": "Bolt rifle", "kind": "Ranged Weapons", "stats": {"Range": '24"'}},
        ],
        model_composition=[
            {
                "key": "sergeant",
                "name": "Intercessor Sergeant",
                "min_models": 1,
                "max_models": 1,
                "wargear_options": [
                    {"key": "power-fist", "name": "Power fist", "kind": "Melee Weapons", "stats": {"A": "3"}},
                ],
                "composition_options": ["Sergeant"],
            }
        ],
    )

    response = client.post(
        "/api/inventory",
        json={
            "unit_id": unit_id,
            "quantity": 1,
            "models_owned": 5,
            "wargear_selections": {
                "bolt-rifle": 4,
                "sergeant::power-fist": 1,
                "ignored-zero": 0,
            },
        },
    )

    assert response.status_code == 201
    item = response.json()
    assert item["unit_name"] == "Intercessor Squad"
    assert item["faction"] == "Space Marines"
    assert item["wargear"] == "1x Intercessor Sergeant: Power fist, 4x Bolt rifle"
    assert item["wargear_selections"] == {"bolt-rifle": 4, "sergeant::power-fist": 1}
    assert item["wargear_options"][0]["name"] == "Bolt rifle"
    assert item["model_composition"][0]["wargear_option_count"] == 1


def test_inventory_image_upload_download_and_delete(client):
    item = client.post(
        "/api/inventory",
        json={"unit_name": "Photo Test", "quantity": 1, "models_owned": 1},
    ).json()
    copy_id = item["copies"][0]["id"]

    upload_response = client.post(
        f"/api/inventory/{item['id']}/copies/{copy_id}/images",
        data={"image_role": "painted", "caption": " Done "},
        files={"image": ("finished.png", b"not really a png", "image/png")},
    )
    assert upload_response.status_code == 201
    image = upload_response.json()
    assert image["image_role"] == "painted"
    assert image["caption"] == "Done"
    assert image["original_name"] == "finished.png"

    refreshed = client.get("/api/inventory").json()[0]
    assert refreshed["copies"][0]["images"][0]["id"] == image["id"]

    download_response = client.get(image["url"])
    assert download_response.status_code == 200
    assert download_response.content == b"not really a png"

    delete_response = client.delete(f"/api/images/{image['id']}")
    assert delete_response.status_code == 204
    assert client.get(image["url"]).status_code == 404


def test_invalid_inputs_return_client_errors(client):
    assert client.get("/api/status", params={"game_system": "bad-system"}).status_code == 400

    missing_name = client.post("/api/inventory", json={"quantity": 1})
    assert missing_name.status_code == 400
    assert missing_name.json()["detail"] == "unit_name is required for custom inventory items."

    missing_unit = client.post("/api/inventory", json={"unit_id": 999, "quantity": 1})
    assert missing_unit.status_code == 404

    missing_copy = client.put(
        "/api/inventory/123/copies/456",
        json={"models_owned": 1},
    )
    assert missing_copy.status_code == 404

    image = client.post(
        "/api/inventory/123/images",
        files={"image": ("notes.txt", b"text", "text/plain")},
    )
    assert image.status_code == 404


def test_auth_validation_and_small_formatting_helpers(monkeypatch, tmp_path):
    monkeypatch.delenv("WH40K_TEST_FLAG", raising=False)
    assert main._env_flag("WH40K_TEST_FLAG", default=True) is True
    monkeypatch.setenv("WH40K_TEST_FLAG", "yes")
    assert main._env_flag("WH40K_TEST_FLAG") is True
    monkeypatch.setenv("WH40K_TEST_FLAG", "no")
    assert main._env_flag("WH40K_TEST_FLAG") is False

    password_hash = main._password_hash("correct horse")
    assert main._verify_password("correct horse", password_hash) is True
    assert main._verify_password("wrong horse", password_hash) is False
    assert main._verify_password("anything", "not-a-valid-hash") is False
    assert main._verify_password("anything", password_hash.replace("pbkdf2_sha256", "unknown", 1)) is False
    assert len(main._session_token_hash("token")) == 64

    assert main._clean_username(" Test.User ") == "test.user"
    with pytest.raises(HTTPException) as exc_info:
        main._clean_username("no")
    assert exc_info.value.status_code == 400

    assert main._password_error(None) == "Password must be at least 10 characters."
    assert main._password_error("x" * 201) == "Password is too long."
    assert main._password_error("long-enough") is None
    assert main._safe_next_url("/inventory?unit=1") == "/inventory?unit=1"
    assert main._safe_next_url("https://example.com") == "/"
    assert main._safe_next_url("//example.com") == "/"
    assert main._inventory_owner_clause(None) == ("i.owner_user_id IS NULL", [])
    assert main._inventory_owner_clause(7, alias="") == ("owner_user_id = ?", [7])

    assert main._decode_wargear_options(None) == []
    assert main._decode_wargear_options("{bad") == []
    assert main._wargear_options_from_payload("bad") == []
    assert main._wargear_options_from_payload([{"key": " k ", "name": " Name ", "stats": {"A": 3, "B": None}}]) == [
        {"key": "k", "name": "Name", "kind": "Weapon", "stats": {"A": "3"}}
    ]
    assert main._decode_model_composition("{bad") == []
    assert main._decode_model_composition(json.dumps({"not": "a list"})) == []
    assert main._safe_optional_int("-1") is None
    assert main._safe_optional_int("bad") is None
    assert main._clean_wargear_selections("bad") == {}
    assert main._clean_wargear_selections({"": 2, "large": 1200, "bad": "many"}) == {"large": 999}
    assert main._decode_wargear_selections("{bad") == {}

    assert main._wargear_text(None) == ""
    assert main._wargear_text("plain notes") == "plain notes"
    assert main._wargear_text(json.dumps(["not", "dict"])) == json.dumps(["not", "dict"])
    assert main._wargear_text(
        json.dumps(
            {
                "selected": [
                    {"name": "Bolt rifle", "quantity": 2},
                    {"name": "", "quantity": 3},
                    {"name": "Ignored", "quantity": "bad"},
                ],
                "notes": " Extra ammo ",
            }
        )
    ) == "2 x Bolt rifle; Extra ammo"

    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path)
    main._delete_image_file(1, "missing.png")
    assert main._safe_image_role("Painted") == "painted"
    assert main._safe_image_role("invalid") == "other"
    assert main._safe_image_ext(DummyUpload("x.any", "image/webp; charset=binary")) == ".webp"
    assert main._safe_image_ext(DummyUpload("photo.jpeg", "application/octet-stream")) == ".jpg"
    assert main._original_name(None) is None
    assert main._original_name("../" + ("x" * 300) + ".png") == (("x" * 300) + ".png")[:240]
    with pytest.raises(HTTPException) as upload_exc:
        main._safe_image_ext(DummyUpload("notes.txt", "text/plain"))
    assert upload_exc.value.status_code == 400
