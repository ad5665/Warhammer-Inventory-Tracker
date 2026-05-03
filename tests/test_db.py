import sqlite3

import pytest

import app.db as db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "stock_tracker.db")
    return db.DB_PATH


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
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE copies (models_owned)")
    conn.executemany("INSERT INTO copies (models_owned) VALUES (?)", [(3,), ("bad",), (10,)])
    rows = conn.execute("SELECT models_owned FROM copies").fetchall()

    assert db._safe_count(None) == 0
    assert db._safe_count("bad") == 0
    assert db._safe_count("-2") == 0
    assert db._distributed_counts(8, rows) == [3, 5, 0]


def test_init_db_backfills_copy_progress(isolated_db):
    db.init_db()
    now = db.utc_now_sql()
    with db.connect() as conn:
        item_id = conn.execute(
            """
            INSERT INTO inventory_items (
                unit_name, quantity, models_owned, built_count, painted_count,
                created_at, updated_at
            ) VALUES ('Backfill Squad', 2, 10, 7, 5, ?, ?)
            """,
            (now, now),
        ).lastrowid
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
            SELECT copy_number, built_count, painted_count
            FROM inventory_copies
            WHERE inventory_item_id = ?
            ORDER BY copy_number
            """,
            (item_id,),
        ).fetchall()
        assert db.table_count(conn, "inventory_items") == 1

    assert [(row["built_count"], row["painted_count"]) for row in copies] == [(5, 5), (2, 0)]
