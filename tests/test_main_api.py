import csv
import io
import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.db as db
import app.main as main
from app.bsdata import ImportResult, SyncResult


class DummyUpload:
    def __init__(self, filename=None, content_type=None):
        self.filename = filename
        self.content_type = content_type


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "BSDATA_ROOT", data_dir / "bsdata")
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    monkeypatch.setattr(main, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(main, "BSDATA_AUTO_SYNC_ENABLED", False)

    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "BSDATA_ROOT", data_dir / "bsdata")
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(main, "AUTH_COOKIE_SECURE", False)
    monkeypatch.setattr(main, "AUTH_SESSION_DAYS", 1)
    monkeypatch.setattr(main, "INITIAL_ADMIN_USERNAME", "Root.Admin")
    monkeypatch.setattr(main, "BSDATA_AUTO_SYNC_ENABLED", False)
    monkeypatch.setattr(
        main.secrets, "token_urlsafe", lambda length: "temporary-admin-password"
    )

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
                public_id, game_system, bs_id, name, faction, catalogue_file, entry_type,
                points, min_models, max_models, keywords, stats_json,
                wargear_options_json, model_composition_json, active, imported_at
            ) VALUES (
                %s, 'wh40k_10e', 'unit-1', %s, %s, 'Space Marines.cat', 'unit',
                90, 5, 10, 'Infantry, Battleline', %s,
                %s, %s, 1, %s
            )
            RETURNING id
            """,
            (
                db._new_public_id(),
                name,
                faction,
                json.dumps({"T": "4", "SV": "3+"}),
                json.dumps(wargear_options or []),
                json.dumps(model_composition or []),
                now,
            ),
        )
        return cursor.fetchone()["id"]


def test_status_game_systems_units_and_factions(client):
    _insert_catalogue_unit(name="Chaos Lords", faction="Heretic Astartes")

    status = client.get("/api/status").json()
    assert status["game_system"] == "wh40k_10e"
    assert status["unit_count"] == 1
    assert status["inventory_count"] == 0

    systems = client.get("/api/game-systems").json()
    assert {system["id"] for system in systems} >= {
        "wh40k_10e",
        "kill_team",
        "age_of_sigmar_4e",
    }

    factions = client.get("/api/factions").json()
    assert factions == [{"faction": "Heretic Astartes", "unit_count": 1}]

    units = client.get("/api/units", params={"query": "chaos lord"}).json()
    assert len(units) == 1
    assert units[0]["name"] == "Chaos Lords"
    assert units[0]["public_id"]
    assert units[0]["version"] == 1
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
    assert item["public_id"]
    assert item["version"] == 1
    assert item["unbuilt_count"] == 4
    assert item["unpainted_count"] == 6
    assert [copy["copy_number"] for copy in item["copies"]] == [1, 2]
    assert all(copy["public_id"] for copy in item["copies"])
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
    assert copy_response.json()["version"] == 2

    inventory = client.get("/api/inventory").json()
    assert len(inventory) == 1
    assert inventory[0]["built_count"] == 8
    assert inventory[0]["painted_count"] == 5
    assert inventory[0]["version"] == 2

    export_response = client.get("/api/export.csv")
    assert export_response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(export_response.text)))
    assert rows[0]["unit_name"] == "Custom Scouts"
    assert rows[0]["public_id"] == item["public_id"]
    assert rows[0]["built_count"] == "8"
    assert rows[0]["painted_count"] == "5"

    delete_response = client.delete(f"/api/inventory/{item['id']}")
    assert delete_response.status_code == 204
    assert client.get("/api/inventory").json() == []
    with db.connect() as conn:
        deleted = conn.execute(
            "SELECT deleted_at FROM inventory_items WHERE id = %s", (item["id"],)
        ).fetchone()
    assert deleted["deleted_at"] is not None

    import_response = client.post(
        "/api/import.csv",
        files={
            "file": (
                "warhammer_inventory_wh40k_10e.csv",
                export_response.text,
                "text/csv",
            )
        },
    )
    assert import_response.status_code == 200
    assert import_response.json()["updated"] == 1
    imported_inventory = client.get("/api/inventory").json()
    assert len(imported_inventory) == 1
    assert imported_inventory[0]["public_id"] == item["public_id"]
    assert imported_inventory[0]["unit_name"] == "Custom Scouts"
    assert imported_inventory[0]["built_count"] == 8
    assert imported_inventory[0]["painted_count"] == 5

    invalid_import = client.post(
        "/api/import.csv",
        files={"file": ("bad.csv", "unit_name,quantity\nBad,-1\n", "text/csv")},
    )
    assert invalid_import.status_code == 400


def test_inventory_item_update_reseeds_copies_and_handles_missing_item(client):
    item = client.post(
        "/api/inventory",
        json={
            "unit_name": "Update Target",
            "quantity": 1,
            "models_owned": 5,
            "built_count": 2,
            "painted_count": 1,
        },
    ).json()

    response = client.put(
        f"/api/inventory/{item['id']}",
        json={
            "unit_name": "Updated Target",
            "faction": "Adeptus Test",
            "quantity": 2,
            "models_owned": 8,
            "built_count": 6,
            "painted_count": 3,
            "wargear": "Plasma",
            "storage_location": "Case B",
            "notes": "Updated notes",
        },
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["unit_name"] == "Updated Target"
    assert updated["faction"] == "Adeptus Test"
    assert [copy["copy_number"] for copy in updated["copies"]] == [1, 2]
    assert updated["built_count"] == 6
    assert updated["painted_count"] == 3

    missing = client.put(
        "/api/inventory/999", json={"unit_name": "Missing", "quantity": 1}
    )
    assert missing.status_code == 404


def test_inventory_from_catalogue_unit_uses_wargear_selection_summary(client):
    unit_id = _insert_catalogue_unit(
        wargear_options=[
            {
                "key": "bolt-rifle",
                "name": "Bolt rifle",
                "kind": "Ranged Weapons",
                "stats": {"Range": '24"'},
            },
        ],
        model_composition=[
            {
                "key": "sergeant",
                "name": "Intercessor Sergeant",
                "min_models": 1,
                "max_models": 1,
                "wargear_options": [
                    {
                        "key": "power-fist",
                        "name": "Power fist",
                        "kind": "Melee Weapons",
                        "stats": {"A": "3"},
                    },
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
    assert image["public_id"]
    assert image["storage_key"].startswith(f"inventory/{item['public_id']}/")
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
    with db.connect() as conn:
        deleted = conn.execute(
            "SELECT deleted_at FROM inventory_images WHERE id = %s", (image["id"],)
        ).fetchone()
    assert deleted["deleted_at"] is not None


def test_inventory_image_upload_errors(client, monkeypatch):
    item = client.post(
        "/api/inventory",
        json={"unit_name": "Photo Errors", "quantity": 1, "models_owned": 1},
    ).json()

    empty = client.post(
        f"/api/inventory/{item['id']}/images",
        files={"image": ("empty.png", b"", "image/png")},
    )
    assert empty.status_code == 400

    missing_copy = client.post(
        f"/api/inventory/{item['id']}/copies/999/images",
        files={"image": ("copy.png", b"image", "image/png")},
    )
    assert missing_copy.status_code == 404

    monkeypatch.setattr(main, "MAX_IMAGE_BYTES", 4)
    too_large = client.post(
        f"/api/inventory/{item['id']}/images",
        files={"image": ("large.png", b"12345", "image/png")},
    )
    assert too_large.status_code == 413

    assert client.delete("/api/images/999").status_code == 404


def test_invalid_inputs_return_client_errors(client):
    assert (
        client.get("/api/status", params={"game_system": "bad-system"}).status_code
        == 400
    )

    missing_name = client.post("/api/inventory", json={"quantity": 1})
    assert missing_name.status_code == 400
    assert (
        missing_name.json()["detail"]
        == "unit_name is required for custom inventory items."
    )

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


def test_sync_records_success_and_failure(client, monkeypatch):
    def fake_sync_repository(target_dir, config):
        return SyncResult(str(target_dir), "synced", used_git=True)

    def fake_import_bsdata(conn, repo_dir, game_system):
        return ImportResult(
            files_scanned=2, units_imported=3, errors=["bad.cat: invalid"]
        )

    monkeypatch.setattr(main, "sync_repository", fake_sync_repository)
    monkeypatch.setattr(main, "import_bsdata", fake_import_bsdata)

    response = client.post("/api/sync/kill_team")
    assert response.status_code == 200
    assert response.json()["status"] == "success_with_errors"
    assert response.json()["errors"] == ["bad.cat: invalid"]

    def failing_sync_repository(target_dir, config):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(main, "sync_repository", failing_sync_repository)
    failed = client.post("/api/sync", params={"game_system": "kill_team"})
    assert failed.status_code == 500
    assert "network unavailable" in failed.json()["detail"]

    with db.connect() as conn:
        statuses = [
            row["status"]
            for row in conn.execute(
                "SELECT status FROM import_runs ORDER BY id"
            ).fetchall()
        ]
    assert statuses == ["success_with_errors", "failed"]


def test_admin_portal_can_sync_all_bsdata(auth_client, monkeypatch):
    login = auth_client.post(
        "/auth/login",
        data={
            "username": "root.admin",
            "password": "temporary-admin-password",
            "next_url": "/admin",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303
    auth_client.post(
        "/admin/password",
        data={
            "new_password": "permanent-password",
            "confirm_password": "permanent-password",
        },
    )

    calls = []

    def fake_sync_repository(target_dir, config):
        calls.append(config.id)
        return SyncResult(str(target_dir), "synced", used_git=True)

    def fake_import_bsdata(conn, repo_dir, game_system):
        return ImportResult(files_scanned=1, units_imported=2, errors=[])

    monkeypatch.setattr(main, "sync_repository", fake_sync_repository)
    monkeypatch.setattr(main, "import_bsdata", fake_import_bsdata)

    admin_page = auth_client.get("/admin")
    assert "Sync BSData" in admin_page.text

    response = auth_client.post("/admin/bsdata/sync")
    assert response.status_code == 200
    assert "BSData sync completed. Imported 6 entries from 3 files." in response.text
    assert calls == list(main.GAME_SYSTEMS.keys())


def test_bsdata_midnight_delay_helper():
    now = datetime(2026, 5, 3, 23, 30, tzinfo=timezone.utc)
    assert main._seconds_until_next_midnight(now) == 30 * 60


def test_auth_admin_login_password_user_creation_and_logout(auth_client):
    assert auth_client.get("/api/auth/me").status_code == 401
    index_redirect = auth_client.get("/", follow_redirects=False)
    assert index_redirect.status_code == 303
    assert index_redirect.headers["location"].startswith("/login?next=")
    assert "Sign in" in auth_client.get("/login").text

    bad_login = auth_client.post(
        "/auth/login",
        data={"username": "root.admin", "password": "wrong", "next_url": "/api/status"},
    )
    assert "Invalid username or password." in bad_login.text

    login = auth_client.post(
        "/auth/login",
        data={
            "username": "root.admin",
            "password": "temporary-admin-password",
            "next_url": "/api/status",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/admin"
    assert auth_client.get("/api/status").status_code == 403
    assert auth_client.get("/", follow_redirects=False).headers["location"] == "/admin"

    mismatch = auth_client.post(
        "/admin/password",
        data={
            "new_password": "permanent-password",
            "confirm_password": "different-password",
        },
    )
    assert "do not match" in mismatch.text

    password_change = auth_client.post(
        "/admin/password",
        data={
            "new_password": "permanent-password",
            "confirm_password": "permanent-password",
        },
    )
    assert "Password updated." in password_change.text
    assert auth_client.get("/api/status").status_code == 200
    auth_info = auth_client.get("/api/auth/me").json()
    assert auth_info["user"]["must_change_password"] is False
    assert auth_info["preferences"]["theme"] == "default"

    theme_update = auth_client.put(
        "/api/auth/preferences", json={"theme": "night-lords"}
    )
    assert theme_update.status_code == 200
    assert theme_update.json() == {"theme": "night-lords"}
    saved_auth_info = auth_client.get("/api/auth/me").json()
    assert saved_auth_info["preferences"]["theme"] == "night-lords"
    assert saved_auth_info["user"]["preferred_theme"] == "night-lords"
    invalid_theme = auth_client.put("/api/auth/preferences", json={"theme": "tyranids"})
    assert invalid_theme.status_code == 400

    create_user = auth_client.post(
        "/admin/users",
        data={"username": "Scout.User", "password": "scout-password", "is_admin": "1"},
    )
    assert "Created user" in create_user.text
    duplicate = auth_client.post(
        "/admin/users",
        data={"username": "scout.user", "password": "scout-password"},
    )
    assert "Could not create user" in duplicate.text

    logout = auth_client.post("/auth/logout", follow_redirects=False)
    assert logout.status_code == 303
    assert auth_client.get("/api/auth/me").status_code == 401


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
    assert (
        main._verify_password(
            "anything", password_hash.replace("pbkdf2_sha256", "unknown", 1)
        )
        is False
    )
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
    assert main._inventory_owner_clause(None) == (
        "i.owner_user_id IS NULL AND i.deleted_at IS NULL",
        [],
    )
    assert main._inventory_owner_clause(7, alias="") == (
        "owner_user_id = %s AND deleted_at IS NULL",
        [7],
    )
    claims = main._merge_oidc_role_claims(
        {"sub": "user-1", "realm_access": {"roles": ["offline_access"]}},
        {"resource_access": {"wh40k-web": {"roles": ["wh40k-admin"]}}},
    )
    assert "wh40k-admin" in main._oidc_role_names(claims)
    monkeypatch.setenv(
        "WH40K_CSV_VALUES", " http://localhost:5173, ,exp://127.0.0.1:8081 "
    )
    assert main._csv_env_values("WH40K_CSV_VALUES") == [
        "http://localhost:5173",
        "exp://127.0.0.1:8081",
    ]

    assert main._decode_wargear_options(None) == []
    assert main._decode_wargear_options("{bad") == []
    assert main._wargear_options_from_payload("bad") == []
    assert main._wargear_options_from_payload(
        [{"key": " k ", "name": " Name ", "stats": {"A": 3, "B": None}}]
    ) == [{"key": "k", "name": "Name", "kind": "Weapon", "stats": {"A": "3"}}]
    assert main._decode_model_composition("{bad") == []
    assert main._decode_model_composition(json.dumps({"not": "a list"})) == []
    assert main._safe_optional_int("-1") is None
    assert main._safe_optional_int("bad") is None
    assert main._clean_wargear_selections("bad") == {}
    assert main._clean_wargear_selections({"": 2, "large": 1200, "bad": "many"}) == {
        "large": 999
    }
    assert main._decode_wargear_selections("{bad") == {}

    assert main._wargear_text(None) == ""
    assert main._wargear_text("plain notes") == "plain notes"
    assert main._wargear_text(json.dumps(["not", "dict"])) == json.dumps(
        ["not", "dict"]
    )
    assert (
        main._wargear_text(
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
        )
        == "2 x Bolt rifle; Extra ammo"
    )

    main._delete_image_file(1, "missing.png")
    assert main._safe_image_role("Painted") == "painted"
    assert main._safe_image_role("invalid") == "other"
    assert (
        main._safe_image_ext(DummyUpload("x.any", "image/webp; charset=binary"))
        == ".webp"
    )
    assert (
        main._safe_image_ext(DummyUpload("photo.jpeg", "application/octet-stream"))
        == ".jpg"
    )
    assert main._original_name(None) is None
    assert (
        main._original_name("../" + ("x" * 300) + ".png")
        == (("x" * 300) + ".png")[:240]
    )
    with pytest.raises(HTTPException) as upload_exc:
        main._safe_image_ext(DummyUpload("notes.txt", "text/plain"))
    assert upload_exc.value.status_code == 400
