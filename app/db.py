from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("WH40K_DATA_DIR", BASE_DIR / "data"))
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://wh40k:wh40k@127.0.0.1:5432/wh40k")
DB_SCHEMA = os.getenv("WH40K_DB_SCHEMA", "public").strip() or "public"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def utc_now_sql() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def database_label() -> str:
    parsed = urlsplit(DATABASE_URL)
    if not parsed.password:
        return DATABASE_URL

    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    if parsed.username:
        host = f"{parsed.username}:***@{host}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


def _validate_identifier(identifier: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError(f"Unsafe PostgreSQL identifier: {identifier!r}")
    return identifier


def _translate_sql(statement: str) -> str:
    translated = statement.replace("?", "%s")
    translated = re.sub(r"\s+COLLATE\s+NOCASE\b", "", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bLIKE\b", "ILIKE", translated, flags=re.IGNORECASE)
    return translated


def _split_script(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";") if statement.strip()]


class AppConnection:
    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def execute(self, statement: str, params: Any | None = None):
        return self._conn.execute(_translate_sql(statement), params)

    def executemany(self, statement: str, params_seq: Any):
        cursor = self._conn.cursor()
        cursor.executemany(_translate_sql(statement), params_seq)
        return cursor

    def executescript(self, script: str) -> None:
        for statement in _split_script(script):
            self.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def get_connection() -> AppConnection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    schema = _validate_identifier(DB_SCHEMA)
    raw.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    raw.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
    return AppConnection(raw)


@contextmanager
def connect() -> Iterator[AppConnection]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def drop_schema(schema: str) -> None:
    schema = _validate_identifier(schema)
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _column_names(conn: AppConnection, table_name: str) -> set[str]:
    _validate_identifier(table_name)
    rows = conn.execute(
        """
        SELECT column_name AS name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = ?
        """,
        (table_name,),
    ).fetchall()
    return {row["name"] for row in rows}


def _ensure_column(conn: AppConnection, table_name: str, column_name: str, ddl: str) -> None:
    _validate_identifier(table_name)
    _validate_identifier(column_name)
    if column_name not in _column_names(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")


def _safe_count(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _distributed_counts(total: int, copies: list[Any]) -> list[int]:
    remaining = max(total, 0)
    values: list[int] = []
    for copy in copies:
        if remaining <= 0:
            values.append(0)
            continue
        capacity = _safe_count(copy["models_owned"])
        value = min(capacity, remaining) if capacity > 0 else remaining
        values.append(value)
        remaining -= value
    return values


def _backfill_copy_progress(conn: AppConnection) -> None:
    items = conn.execute(
        """
        SELECT id, built_count, painted_count
        FROM inventory_items
        WHERE COALESCE(built_count, 0) > 0
           OR COALESCE(painted_count, 0) > 0
        """
    ).fetchall()
    for item in items:
        copies = conn.execute(
            """
            SELECT id, models_owned, built_count, painted_count
            FROM inventory_copies
            WHERE inventory_item_id = ?
            ORDER BY copy_number
            """,
            (item["id"],),
        ).fetchall()
        if not copies:
            continue

        built_values: list[int] | None = None
        painted_values: list[int] | None = None
        if _safe_count(item["built_count"]) > 0 and sum(_safe_count(copy["built_count"]) for copy in copies) == 0:
            built_values = _distributed_counts(_safe_count(item["built_count"]), copies)
        if _safe_count(item["painted_count"]) > 0 and sum(_safe_count(copy["painted_count"]) for copy in copies) == 0:
            painted_values = _distributed_counts(_safe_count(item["painted_count"]), copies)

        if built_values is None and painted_values is None:
            continue

        for index, copy in enumerate(copies):
            conn.execute(
                """
                UPDATE inventory_copies
                SET built_count = COALESCE(?, built_count),
                    painted_count = COALESCE(?, painted_count)
                WHERE id = ?
                """,
                (
                    built_values[index] if built_values is not None else None,
                    painted_values[index] if painted_values is not None else None,
                    copy["id"],
                ),
            )


def _new_public_id() -> str:
    return str(uuid.uuid4())


def _backfill_public_ids(conn: AppConnection) -> None:
    for table_name in ("bsd_units", "inventory_items", "inventory_copies", "inventory_images"):
        if "public_id" not in _column_names(conn, table_name):
            continue
        rows = conn.execute(
            f"SELECT id FROM {table_name} WHERE public_id IS NULL OR public_id = ''"
        ).fetchall()
        for row in rows:
            conn.execute(
                f"UPDATE {table_name} SET public_id = ? WHERE id = ?",
                (_new_public_id(), row["id"]),
            )


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bsd_units (
                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                public_id TEXT,
                game_system TEXT NOT NULL DEFAULT 'wh40k_10e',
                bs_id TEXT NOT NULL,
                name TEXT NOT NULL,
                faction TEXT NOT NULL,
                catalogue_file TEXT NOT NULL,
                entry_type TEXT,
                points DOUBLE PRECISION,
                min_models INTEGER,
                max_models INTEGER,
                keywords TEXT,
                stats_json TEXT,
                wargear_options_json TEXT,
                model_composition_json TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                imported_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                UNIQUE(game_system, bs_id, catalogue_file)
            );

            CREATE TABLE IF NOT EXISTS inventory_items (
                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                public_id TEXT,
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
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                FOREIGN KEY(unit_id) REFERENCES bsd_units(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_images (
                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                public_id TEXT,
                inventory_item_id INTEGER NOT NULL,
                inventory_copy_id INTEGER,
                file_name TEXT NOT NULL,
                storage_key TEXT,
                original_name TEXT,
                content_type TEXT,
                image_role TEXT NOT NULL DEFAULT 'other',
                caption TEXT,
                created_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                FOREIGN KEY(inventory_item_id) REFERENCES inventory_items(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS inventory_copies (
                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                public_id TEXT,
                inventory_item_id INTEGER NOT NULL,
                copy_number INTEGER NOT NULL CHECK(copy_number >= 1),
                models_owned INTEGER NOT NULL DEFAULT 0 CHECK(models_owned >= 0),
                built_count INTEGER NOT NULL DEFAULT 0 CHECK(built_count >= 0),
                painted_count INTEGER NOT NULL DEFAULT 0 CHECK(painted_count >= 0),
                model_number TEXT,
                wargear TEXT,
                wargear_selections_json TEXT,
                storage_location TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                FOREIGN KEY(inventory_item_id) REFERENCES inventory_items(id) ON DELETE CASCADE,
                UNIQUE(inventory_item_id, copy_number)
            );

            CREATE TABLE IF NOT EXISTS import_runs (
                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
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
                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                auth_provider TEXT NOT NULL DEFAULT 'local',
                auth_subject TEXT,
                email TEXT,
                display_name TEXT,
                preferred_theme TEXT NOT NULL DEFAULT 'default',
                is_admin INTEGER NOT NULL DEFAULT 0,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS auth_sessions (
                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at BIGINT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE
            );
            """
        )

        _ensure_column(conn, "bsd_units", "public_id", "public_id TEXT")
        _ensure_column(conn, "bsd_units", "game_system", "game_system TEXT NOT NULL DEFAULT 'wh40k_10e'")
        _ensure_column(conn, "bsd_units", "min_models", "min_models INTEGER")
        _ensure_column(conn, "bsd_units", "max_models", "max_models INTEGER")
        _ensure_column(conn, "bsd_units", "version", "version INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "bsd_units", "deleted_at", "deleted_at TEXT")
        _ensure_column(conn, "inventory_items", "public_id", "public_id TEXT")
        _ensure_column(conn, "inventory_items", "owner_user_id", "owner_user_id INTEGER")
        _ensure_column(conn, "inventory_items", "game_system", "game_system TEXT NOT NULL DEFAULT 'wh40k_10e'")
        _ensure_column(conn, "bsd_units", "wargear_options_json", "wargear_options_json TEXT")
        _ensure_column(conn, "bsd_units", "model_composition_json", "model_composition_json TEXT")
        _ensure_column(conn, "inventory_items", "wargear", "wargear TEXT")
        _ensure_column(conn, "inventory_items", "wargear_selections_json", "wargear_selections_json TEXT")
        _ensure_column(conn, "inventory_items", "model_number", "model_number TEXT")
        _ensure_column(conn, "inventory_items", "version", "version INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "inventory_items", "deleted_at", "deleted_at TEXT")
        _ensure_column(conn, "inventory_copies", "public_id", "public_id TEXT")
        _ensure_column(conn, "inventory_copies", "models_owned", "models_owned INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "inventory_copies", "built_count", "built_count INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "inventory_copies", "painted_count", "painted_count INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "inventory_copies", "version", "version INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "inventory_copies", "deleted_at", "deleted_at TEXT")
        _ensure_column(conn, "inventory_images", "public_id", "public_id TEXT")
        _ensure_column(conn, "inventory_images", "inventory_copy_id", "inventory_copy_id INTEGER")
        _ensure_column(conn, "inventory_images", "storage_key", "storage_key TEXT")
        _ensure_column(conn, "inventory_images", "version", "version INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "inventory_images", "deleted_at", "deleted_at TEXT")
        _ensure_column(conn, "import_runs", "game_system", "game_system TEXT NOT NULL DEFAULT 'wh40k_10e'")
        _ensure_column(conn, "auth_users", "auth_provider", "auth_provider TEXT NOT NULL DEFAULT 'local'")
        _ensure_column(conn, "auth_users", "auth_subject", "auth_subject TEXT")
        _ensure_column(conn, "auth_users", "email", "email TEXT")
        _ensure_column(conn, "auth_users", "display_name", "display_name TEXT")
        _ensure_column(conn, "auth_users", "preferred_theme", "preferred_theme TEXT NOT NULL DEFAULT 'default'")

        _backfill_public_ids(conn)

        conn.execute(
            """
            UPDATE inventory_copies
            SET models_owned = (
                SELECT COALESCE(i.models_owned, 0)
                FROM inventory_items i
                WHERE i.id = inventory_copies.inventory_item_id
            )
            WHERE COALESCE(models_owned, 0) = 0
            """
        )
        _backfill_copy_progress(conn)

        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_bsd_units_game_name
                ON bsd_units(game_system, lower(name));
            CREATE INDEX IF NOT EXISTS idx_bsd_units_game_faction
                ON bsd_units(game_system, lower(faction));
            CREATE INDEX IF NOT EXISTS idx_bsd_units_game_active
                ON bsd_units(game_system, active);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_bsd_units_public_id
                ON bsd_units(public_id)
                WHERE public_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_bsd_units_deleted_at
                ON bsd_units(deleted_at);

            CREATE INDEX IF NOT EXISTS idx_inventory_game_unit_id
                ON inventory_items(game_system, unit_id);
            CREATE INDEX IF NOT EXISTS idx_inventory_game_unit_name
                ON inventory_items(game_system, lower(unit_name));
            CREATE INDEX IF NOT EXISTS idx_inventory_model_number
                ON inventory_items(game_system, lower(model_number));
            CREATE INDEX IF NOT EXISTS idx_inventory_owner_game
                ON inventory_items(owner_user_id, game_system);
            CREATE INDEX IF NOT EXISTS idx_inventory_owner_game_name
                ON inventory_items(owner_user_id, game_system, lower(unit_name));
            CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_public_id
                ON inventory_items(public_id)
                WHERE public_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_inventory_deleted_at
                ON inventory_items(deleted_at);

            CREATE INDEX IF NOT EXISTS idx_inventory_copies_item
                ON inventory_copies(inventory_item_id, copy_number);
            CREATE INDEX IF NOT EXISTS idx_inventory_copies_model_number
                ON inventory_copies(lower(model_number));
            CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_copies_public_id
                ON inventory_copies(public_id)
                WHERE public_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_inventory_copies_deleted_at
                ON inventory_copies(deleted_at);

            CREATE INDEX IF NOT EXISTS idx_inventory_images_item
                ON inventory_images(inventory_item_id);
            CREATE INDEX IF NOT EXISTS idx_inventory_images_copy
                ON inventory_images(inventory_copy_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_images_public_id
                ON inventory_images(public_id)
                WHERE public_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_inventory_images_deleted_at
                ON inventory_images(deleted_at);
            CREATE INDEX IF NOT EXISTS idx_import_runs_game
                ON import_runs(game_system, id);
            CREATE INDEX IF NOT EXISTS idx_auth_users_username
                ON auth_users(lower(username));
            CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_users_provider_subject
                ON auth_users(auth_provider, auth_subject)
                WHERE auth_subject IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_token
                ON auth_sessions(token_hash);
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
                ON auth_sessions(user_id);
            """
        )


def table_count(conn: AppConnection, table_name: str) -> int:
    _validate_identifier(table_name)
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
    return int(row["count"] if row else 0)
