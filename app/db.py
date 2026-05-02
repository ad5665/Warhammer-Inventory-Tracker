from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("WH40K_DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.getenv("WH40K_DB_PATH", DATA_DIR / "stock_tracker.db"))


def utc_now_sql() -> str:
    # SQLite-friendly UTC timestamp.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
    if column_name not in _column_names(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bsd_units (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_system TEXT NOT NULL DEFAULT 'wh40k_10e',
                bs_id TEXT NOT NULL,
                name TEXT NOT NULL,
                faction TEXT NOT NULL,
                catalogue_file TEXT NOT NULL,
                entry_type TEXT,
                points REAL,
                min_models INTEGER,
                max_models INTEGER,
                keywords TEXT,
                stats_json TEXT,
                wargear_options_json TEXT,
                model_composition_json TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                imported_at TEXT NOT NULL,
                UNIQUE(game_system, bs_id, catalogue_file)
            );

            CREATE TABLE IF NOT EXISTS inventory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER,
                game_system TEXT NOT NULL DEFAULT 'wh40k_10e',
                unit_id INTEGER,
                unit_name TEXT NOT NULL,
                faction TEXT,
                catalogue_file TEXT,
                quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity >= 0),
                models_owned INTEGER NOT NULL DEFAULT 0 CHECK(models_owned >= 0),
                built_count INTEGER NOT NULL DEFAULT 0 CHECK(built_count >= 0),
                painted_count INTEGER NOT NULL DEFAULT 0 CHECK(painted_count >= 0),
                wargear TEXT,
                wargear_selections_json TEXT,
                model_number TEXT,
                storage_location TEXT,
                notes TEXT,
                acquired_on TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(unit_id) REFERENCES bsd_units(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inventory_item_id INTEGER NOT NULL,
                inventory_copy_id INTEGER,
                file_name TEXT NOT NULL,
                original_name TEXT,
                content_type TEXT,
                image_role TEXT NOT NULL DEFAULT 'other',
                caption TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(inventory_item_id) REFERENCES inventory_items(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS inventory_copies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inventory_item_id INTEGER NOT NULL,
                copy_number INTEGER NOT NULL CHECK(copy_number >= 1),
                model_number TEXT,
                wargear TEXT,
                wargear_selections_json TEXT,
                storage_location TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(inventory_item_id) REFERENCES inventory_items(id) ON DELETE CASCADE,
                UNIQUE(inventory_item_id, copy_number)
            );

            CREATE TABLE IF NOT EXISTS import_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_system TEXT NOT NULL DEFAULT 'wh40k_10e',
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                message TEXT,
                repo_message TEXT,
                files_scanned INTEGER NOT NULL DEFAULT 0,
                units_imported INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT
            );

            CREATE TABLE IF NOT EXISTS auth_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS auth_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE
            );
            """
        )

        # Lightweight migrations for databases created by earlier versions of
        # the app. SQLite can add simple columns without rebuilding the table.
        _ensure_column(conn, "bsd_units", "game_system", "game_system TEXT NOT NULL DEFAULT 'wh40k_10e'")
        _ensure_column(conn, "bsd_units", "min_models", "min_models INTEGER")
        _ensure_column(conn, "bsd_units", "max_models", "max_models INTEGER")
        _ensure_column(conn, "inventory_items", "owner_user_id", "owner_user_id INTEGER")
        _ensure_column(conn, "inventory_items", "game_system", "game_system TEXT NOT NULL DEFAULT 'wh40k_10e'")
        _ensure_column(conn, "bsd_units", "wargear_options_json", "wargear_options_json TEXT")
        _ensure_column(conn, "bsd_units", "model_composition_json", "model_composition_json TEXT")
        _ensure_column(conn, "inventory_items", "wargear", "wargear TEXT")
        _ensure_column(conn, "inventory_items", "wargear_selections_json", "wargear_selections_json TEXT")
        _ensure_column(conn, "inventory_items", "model_number", "model_number TEXT")
        _ensure_column(conn, "inventory_images", "inventory_copy_id", "inventory_copy_id INTEGER")
        _ensure_column(conn, "import_runs", "game_system", "game_system TEXT NOT NULL DEFAULT 'wh40k_10e'")

        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_bsd_units_game_name
                ON bsd_units(game_system, name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_bsd_units_game_faction
                ON bsd_units(game_system, faction COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_bsd_units_game_active
                ON bsd_units(game_system, active);

            CREATE INDEX IF NOT EXISTS idx_inventory_game_unit_id
                ON inventory_items(game_system, unit_id);
            CREATE INDEX IF NOT EXISTS idx_inventory_game_unit_name
                ON inventory_items(game_system, unit_name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_inventory_model_number
                ON inventory_items(game_system, model_number COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_inventory_owner_game
                ON inventory_items(owner_user_id, game_system);
            CREATE INDEX IF NOT EXISTS idx_inventory_owner_game_name
                ON inventory_items(owner_user_id, game_system, unit_name COLLATE NOCASE);

            CREATE INDEX IF NOT EXISTS idx_inventory_copies_item
                ON inventory_copies(inventory_item_id, copy_number);
            CREATE INDEX IF NOT EXISTS idx_inventory_copies_model_number
                ON inventory_copies(model_number COLLATE NOCASE);

            CREATE INDEX IF NOT EXISTS idx_inventory_images_item
                ON inventory_images(inventory_item_id);
            CREATE INDEX IF NOT EXISTS idx_inventory_images_copy
                ON inventory_images(inventory_copy_id);
            CREATE INDEX IF NOT EXISTS idx_import_runs_game
                ON import_runs(game_system, id);
            CREATE INDEX IF NOT EXISTS idx_auth_users_username
                ON auth_users(username COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_token
                ON auth_sessions(token_hash);
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
                ON auth_sessions(user_id);
            """
        )


def table_count(conn: sqlite3.Connection, table_name: str) -> int:
    # table_name is controlled by the application, not user input.
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
    return int(row["count"] if row else 0)
