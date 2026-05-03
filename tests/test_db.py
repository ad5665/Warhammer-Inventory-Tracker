import pytest

import app.db as db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    return data_dir


def test_connect_commits_and_rolls_back(isolated_db):
    with db.connect() as conn:
        conn.execute("CREATE TABLE items (name TEXT)")
        conn.execute("INSERT INTO items (name) VALUES ('committed')")

    with db.connect() as conn:
        assert conn.execute("SELECT name FROM items").fetchone()["name"] == "committed"

    with pytest.raises(RuntimeError):
        with db.connect() as conn:
            conn.execute("INSERT INTO items (name) VALUES ('rolled back')")
            raise RuntimeError("abort transaction")

    with db.connect() as conn:
        rows = conn.execute("SELECT name FROM items").fetchall()
    assert [row["name"] for row in rows] == ["committed"]


def test_ensure_column_adds_missing_column(isolated_db):
    with db.connect() as conn:
        conn.execute("CREATE TABLE units (id INTEGER PRIMARY KEY)")
        db._ensure_column(conn, "units", "name", "name TEXT")
        db._ensure_column(conn, "units", "name", "name TEXT")
        assert "name" in db._column_names(conn, "units")


def test_safe_and_distributed_counts_handle_bad_values():
    rows = [{"models_owned": 3}, {"models_owned": "bad"}, {"models_owned": 10}]

    assert db._safe_count(None) == 0
    assert db._safe_count("bad") == 0
    assert db._safe_count("-2") == 0
    assert db._distributed_counts(8, rows) == [3, 5, 0]


def test_init_db_backfills_copy_progress(isolated_db):
    db.init_db()
    now = db.utc_now_sql()
    with db.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO inventory_items (
                unit_name, quantity, models_owned, built_count, painted_count,
                created_at, updated_at
            ) VALUES ('Backfill Squad', 2, 10, 7, 5, ?, ?)
            RETURNING id
            """,
            (now, now),
        )
        item_id = cursor.fetchone()["id"]
        conn.executemany(
            """
            INSERT INTO inventory_copies (
                inventory_item_id, copy_number, models_owned, built_count, painted_count,
                created_at, updated_at
            ) VALUES (?, ?, ?, 0, 0, ?, ?)
            """,
            [
                (item_id, 1, 5, now, now),
                (item_id, 2, 5, now, now),
            ],
        )

    db.init_db()

    with db.connect() as conn:
        copies = conn.execute(
            """
            SELECT copy_number, public_id, built_count, painted_count
            FROM inventory_copies
            WHERE inventory_item_id = ?
            ORDER BY copy_number
            """,
            (item_id,),
        ).fetchall()
        item = conn.execute(
            "SELECT public_id, version, deleted_at FROM inventory_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        assert db.table_count(conn, "inventory_items") == 1

    assert item["public_id"]
    assert item["version"] == 1
    assert item["deleted_at"] is None
    assert all(row["public_id"] for row in copies)
    assert [(row["built_count"], row["painted_count"]) for row in copies] == [(5, 5), (2, 0)]
