from __future__ import annotations

import csv
import io
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .bsdata import (
    DEFAULT_GAME_SYSTEM,
    GAME_SYSTEMS,
    GameSystemConfig,
    UnknownGameSystem,
    get_game_system_config,
    import_bsdata,
    sync_repository,
)
from .db import DATA_DIR, DB_PATH, connect, init_db, table_count, utc_now_sql

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
BSDATA_ROOT = DATA_DIR / "bsdata"
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_IMAGE_BYTES = 12 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
ALLOWED_IMAGE_ROLES = {"built", "painted", "wip", "reference", "other"}


class InventoryPayload(BaseModel):
    game_system: str = DEFAULT_GAME_SYSTEM
    unit_id: int | None = None
    unit_name: str | None = None
    faction: str | None = None
    catalogue_file: str | None = None
    quantity: int = Field(default=1, ge=0)
    models_owned: int = Field(default=0, ge=0)
    built_count: int = Field(default=0, ge=0)
    painted_count: int = Field(default=0, ge=0)
    wargear: str | None = None
    wargear_selections: dict[str, int] | None = None
    model_number: str | None = None
    storage_location: str | None = None
    notes: str | None = None
    acquired_on: str | None = None


class InventoryCopyPayload(BaseModel):
    model_number: str | None = None
    wargear: str | None = None
    wargear_selections: dict[str, int] | None = None
    storage_location: str | None = None
    notes: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Warhammer Stock Tracker",
    description="Track owned Warhammer 40,000, Kill Team, and Age of Sigmar models using BSData catalogue imports and SQLite.",
    version="0.3.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _config_or_400(game_system: str | None) -> GameSystemConfig:
    try:
        return get_game_system_config(game_system)
    except UnknownGameSystem as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _bsdata_dir(config: GameSystemConfig) -> Path:
    return BSDATA_ROOT / config.repo_slug


def _decode_wargear_options(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []

    options: list[dict[str, Any]] = []
    for option in decoded:
        if not isinstance(option, dict):
            continue
        key = _clean_optional(str(option.get("key") or ""))
        name = _clean_optional(str(option.get("name") or ""))
        if not key or not name:
            continue
        stats = option.get("stats") if isinstance(option.get("stats"), dict) else {}
        options.append({
            "key": key,
            "name": name,
            "kind": _clean_optional(str(option.get("kind") or "")) or "Weapon",
            "stats": {str(k): str(v) for k, v in stats.items() if v is not None},
        })
    return options


def _clean_wargear_selections(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, int] = {}
    for raw_key, raw_value in value.items():
        key = _clean_optional(str(raw_key))
        if not key:
            continue
        try:
            amount = int(raw_value)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            cleaned[key[:120]] = min(amount, 999)
    return cleaned


def _decode_wargear_selections(raw: str | None) -> dict[str, int]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return _clean_wargear_selections(decoded)


def _format_wargear_summary(selections: dict[str, int], options: list[dict[str, Any]]) -> str | None:
    if not selections:
        return None
    options_by_key = {option["key"]: option for option in options}
    parts: list[str] = []
    for key in sorted(selections, key=lambda item: options_by_key.get(item, {}).get("name", item).lower()):
        amount = selections[key]
        name = options_by_key.get(key, {}).get("name") or key
        parts.append(f"{amount}x {name}")
    return ", ".join(parts) or None


def _decode_stats(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.pop("stats_json", None)
    if not raw:
        row["stats"] = {}
    else:
        try:
            row["stats"] = json.loads(raw)
        except json.JSONDecodeError:
            row["stats"] = {}

    options = _decode_wargear_options(row.pop("wargear_options_json", None))
    row["wargear_option_count"] = len(options)
    return row


def _wargear_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value

    if not isinstance(parsed, dict):
        return value

    parts: list[str] = []
    selected = parsed.get("selected")
    if isinstance(selected, list):
        for item in selected:
            if not isinstance(item, dict):
                continue
            name = _clean_optional(str(item.get("name") or ""))
            if not name:
                continue
            try:
                quantity = int(item.get("quantity") or 0)
            except (TypeError, ValueError):
                quantity = 0
            if quantity > 0:
                parts.append(f"{quantity} x {name}")

    notes = _clean_textarea(parsed.get("notes") if isinstance(parsed.get("notes"), str) else None)
    if notes:
        parts.append(notes)

    return "; ".join(parts)


def _image_url(row: dict[str, Any]) -> str:
    return f"/uploads/inventory/{row['inventory_item_id']}/{row['file_name']}"


def _image_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["url"] = _image_url(item)
    return item


def _image_path(inventory_item_id: int, file_name: str) -> Path:
    return UPLOAD_DIR / "inventory" / str(inventory_item_id) / Path(file_name).name


def _delete_image_file(inventory_item_id: int, file_name: str) -> None:
    try:
        _image_path(inventory_item_id, file_name).unlink()
    except FileNotFoundError:
        pass


def _copy_payload_data(payload: InventoryCopyPayload, wargear_options: list[dict[str, Any]]) -> dict[str, Any]:
    data = payload.model_dump()
    data["model_number"] = _clean_optional(data.get("model_number"))
    data["storage_location"] = _clean_optional(data.get("storage_location"))
    data["wargear"] = _clean_textarea(data.get("wargear"))
    data["notes"] = _clean_textarea(data.get("notes"))
    data["wargear_selections"] = _clean_wargear_selections(data.get("wargear_selections"))
    data["wargear_selections_json"] = json.dumps(data["wargear_selections"], sort_keys=True) if data["wargear_selections"] else None

    if data["wargear_selections"]:
        data["wargear"] = _format_wargear_summary(data["wargear_selections"], wargear_options)

    return data


def _inventory_copy_dict(row: Any) -> dict[str, Any]:
    copy = dict(row)
    copy["wargear_selections"] = _decode_wargear_selections(copy.pop("wargear_selections_json", None))
    copy.setdefault("images", [])
    return copy


def _copy_seed_from_item(item: Any, copy_number: int) -> dict[str, Any]:
    if copy_number != 1:
        return {
            "model_number": None,
            "wargear": None,
            "wargear_selections_json": None,
            "storage_location": None,
            "notes": None,
        }

    return {
        "model_number": item["model_number"],
        "wargear": item["wargear"],
        "wargear_selections_json": item["wargear_selections_json"],
        "storage_location": item["storage_location"],
        "notes": item["notes"],
    }


def _ensure_inventory_copies(conn: Any, item: Any) -> None:
    item_id = int(item["id"])
    quantity = max(int(item["quantity"] or 0), 0)
    existing_rows = conn.execute(
        "SELECT copy_number FROM inventory_copies WHERE inventory_item_id = ?",
        (item_id,),
    ).fetchall()
    existing_numbers = {int(row["copy_number"]) for row in existing_rows}
    now = utc_now_sql()

    for copy_number in range(1, quantity + 1):
        if copy_number in existing_numbers:
            continue
        seed = _copy_seed_from_item(item, copy_number if existing_numbers else copy_number)
        conn.execute(
            """
            INSERT INTO inventory_copies (
                inventory_item_id, copy_number, model_number, wargear,
                wargear_selections_json, storage_location, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                copy_number,
                seed["model_number"],
                seed["wargear"],
                seed["wargear_selections_json"],
                seed["storage_location"],
                seed["notes"],
                now,
                now,
            ),
        )

    if quantity > 0:
        first_copy = conn.execute(
            """
            SELECT id FROM inventory_copies
            WHERE inventory_item_id = ? AND copy_number = 1
            """,
            (item_id,),
        ).fetchone()
        if first_copy is not None:
            conn.execute(
                """
                UPDATE inventory_images
                SET inventory_copy_id = ?
                WHERE inventory_item_id = ? AND inventory_copy_id IS NULL
                """,
                (first_copy["id"], item_id),
            )


def _trim_inventory_copies(conn: Any, item_id: int, quantity: int) -> list[Any]:
    # Keep out-of-range copy details so accidental quantity reductions do not
    # destroy per-copy notes/photos. The list response only exposes copies up
    # to the current quantity, and raising quantity later reveals them again.
    return []


def _inventory_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["unbuilt_count"] = max(int(item.get("models_owned") or 0) - int(item.get("built_count") or 0), 0)
    item["unpainted_count"] = max(int(item.get("models_owned") or 0) - int(item.get("painted_count") or 0), 0)
    item["wargear_selections"] = _decode_wargear_selections(item.pop("wargear_selections_json", None))
    item["wargear_options"] = _decode_wargear_options(item.pop("current_wargear_options_json", None))
    item.setdefault("images", [])
    return item


def _attach_copies(conn: Any, items: list[dict[str, Any]]) -> None:
    for item in items:
        item["copies"] = []
    if not items:
        return

    ids = [int(item["id"]) for item in items]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT c.*
        FROM inventory_copies c
        JOIN inventory_items i ON i.id = c.inventory_item_id
        WHERE c.inventory_item_id IN ({placeholders})
          AND c.copy_number <= i.quantity
        ORDER BY c.inventory_item_id, c.copy_number
        """,
        ids,
    ).fetchall()
    by_item = {int(item["id"]): item for item in items}
    for row in rows:
        copy = _inventory_copy_dict(row)
        parent = by_item.get(int(copy["inventory_item_id"]))
        if parent is not None:
            parent["copies"].append(copy)


def _attach_images(conn: Any, items: list[dict[str, Any]]) -> None:
    for item in items:
        item["images"] = []
        for copy in item.get("copies", []):
            copy["images"] = []
    if not items:
        return

    ids = [int(item["id"]) for item in items]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT * FROM inventory_images
        WHERE inventory_item_id IN ({placeholders})
        ORDER BY created_at DESC, id DESC
        """,
        ids,
    ).fetchall()
    by_item = {int(item["id"]): item for item in items}
    by_copy = {
        int(copy["id"]): copy
        for item in items
        for copy in item.get("copies", [])
    }
    for row in rows:
        image = _image_dict(row)
        copy_id = image.get("inventory_copy_id")
        if copy_id is not None and int(copy_id) in by_copy:
            by_copy[int(copy_id)]["images"].append(image)
        else:
            parent = by_item.get(int(image["inventory_item_id"]))
            if parent is not None:
                parent["images"].append(image)


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _clean_textarea(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _payload_with_unit_snapshot(payload: InventoryPayload, conn: Any) -> dict[str, Any]:
    data = payload.model_dump()
    config = _config_or_400(data.get("game_system"))
    data["game_system"] = config.id
    data["unit_name"] = _clean_optional(data.get("unit_name"))
    data["faction"] = _clean_optional(data.get("faction"))
    data["catalogue_file"] = _clean_optional(data.get("catalogue_file"))
    data["storage_location"] = _clean_optional(data.get("storage_location"))
    data["acquired_on"] = _clean_optional(data.get("acquired_on"))
    data["model_number"] = _clean_optional(data.get("model_number"))
    data["wargear"] = _clean_textarea(data.get("wargear"))
    data["wargear_selections"] = _clean_wargear_selections(data.get("wargear_selections"))
    data["wargear_selections_json"] = json.dumps(data["wargear_selections"], sort_keys=True) if data["wargear_selections"] else None
    data["notes"] = _clean_textarea(data.get("notes"))

    wargear_options: list[dict[str, Any]] = []
    if data.get("unit_id") is not None:
        unit = conn.execute(
            "SELECT id, game_system, name, faction, catalogue_file, wargear_options_json FROM bsd_units WHERE id = ?",
            (data["unit_id"],),
        ).fetchone()
        if unit is None:
            raise HTTPException(status_code=404, detail="Catalogue entry not found. Sync BSData, then search again.")
        data["game_system"] = unit["game_system"]
        data["unit_name"] = unit["name"]
        data["faction"] = data["faction"] or unit["faction"]
        data["catalogue_file"] = data["catalogue_file"] or unit["catalogue_file"]
        wargear_options = _decode_wargear_options(unit["wargear_options_json"])

    if data["wargear_selections"]:
        data["wargear"] = _format_wargear_summary(data["wargear_selections"], wargear_options)

    if not data.get("unit_name"):
        raise HTTPException(status_code=400, detail="unit_name is required for custom inventory items.")

    return data


def _inventory_item_response(conn: Any, item_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            i.*,
            u.points AS current_points,
            u.keywords AS current_keywords,
            u.stats_json AS current_stats_json,
            u.wargear_options_json AS current_wargear_options_json,
            u.active AS unit_active
        FROM inventory_items i
        LEFT JOIN bsd_units u ON u.id = i.unit_id
        WHERE i.id = ?
        """,
        (item_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Inventory item not found.")

    _ensure_inventory_copies(conn, row)
    item = _inventory_dict(row)
    stats_json = item.pop("current_stats_json", None)
    try:
        item["current_stats"] = json.loads(stats_json) if stats_json else {}
    except json.JSONDecodeError:
        item["current_stats"] = {}
    _attach_copies(conn, [item])
    _attach_images(conn, [item])
    return item


def _item_wargear_options(conn: Any, item_id: int) -> list[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT u.wargear_options_json
        FROM inventory_items i
        LEFT JOIN bsd_units u ON u.id = i.unit_id
        WHERE i.id = ?
        """,
        (item_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Inventory item not found.")
    return _decode_wargear_options(row["wargear_options_json"])


def _safe_image_role(image_role: str | None) -> str:
    cleaned = _clean_optional(image_role) or "other"
    cleaned = cleaned.lower().replace(" ", "_")
    return cleaned if cleaned in ALLOWED_IMAGE_ROLES else "other"


def _safe_image_ext(upload: UploadFile) -> str:
    content_type = (upload.content_type or "").split(";", 1)[0].lower()
    if content_type in ALLOWED_IMAGE_TYPES:
        return ALLOWED_IMAGE_TYPES[content_type]

    suffix = Path(upload.filename or "").suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix

    raise HTTPException(status_code=400, detail="Upload must be a JPG, PNG, WebP, or GIF image.")


def _original_name(filename: str | None) -> str | None:
    if not filename:
        return None
    cleaned = Path(filename).name.strip()
    return cleaned[:240] or None


@app.get("/api/game-systems")
def game_systems() -> list[dict[str, Any]]:
    return [
        {
            "id": config.id,
            "label": config.label,
            "short_label": config.short_label,
            "repo_url": config.repo_http_url,
            "catalogue_word": config.catalogue_word,
        }
        for config in GAME_SYSTEMS.values()
    ]


@app.get("/api/status")
def status(game_system: str = Query(default=DEFAULT_GAME_SYSTEM)) -> dict[str, Any]:
    config = _config_or_400(game_system)
    bsdata_dir = _bsdata_dir(config)
    with connect() as conn:
        last_run = conn.execute(
            "SELECT * FROM import_runs WHERE game_system = ? ORDER BY id DESC LIMIT 1",
            (config.id,),
        ).fetchone()
        active_count = conn.execute(
            "SELECT COUNT(*) AS count FROM bsd_units WHERE game_system = ? AND active = 1",
            (config.id,),
        ).fetchone()["count"]
        unit_count = conn.execute(
            "SELECT COUNT(*) AS count FROM bsd_units WHERE game_system = ?",
            (config.id,),
        ).fetchone()["count"]
        inventory_count = conn.execute(
            "SELECT COUNT(*) AS count FROM inventory_items WHERE game_system = ?",
            (config.id,),
        ).fetchone()["count"]
        image_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM inventory_images img
            JOIN inventory_items i ON i.id = img.inventory_item_id
            WHERE i.game_system = ?
            """,
            (config.id,),
        ).fetchone()["count"]
        return {
            "game_system": config.id,
            "game_label": config.label,
            "database_path": str(DB_PATH),
            "data_dir": str(DATA_DIR),
            "bsdata_dir": str(bsdata_dir),
            "bsdata_present": bsdata_dir.exists(),
            "unit_count": unit_count,
            "active_unit_count": active_count,
            "inventory_count": inventory_count,
            "image_count": image_count,
            "last_import": dict(last_run) if last_run else None,
            "total_database_units": table_count(conn, "bsd_units"),
        }


def _sync_game_system(config: GameSystemConfig) -> dict[str, Any]:
    started_at = utc_now_sql()
    repo_message = ""
    bsdata_dir = _bsdata_dir(config)
    try:
        sync_result = sync_repository(bsdata_dir, config)
        repo_message = sync_result.message
        with connect() as conn:
            import_result = import_bsdata(conn, bsdata_dir, config.id)
            status_text = "success_with_errors" if import_result.errors else "success"
            finished_at = utc_now_sql()
            conn.execute(
                """
                INSERT INTO import_runs (
                    game_system, started_at, finished_at, status, message, repo_message,
                    files_scanned, units_imported, errors_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    config.id,
                    started_at,
                    finished_at,
                    status_text,
                    f"{config.label} BSData sync and import completed.",
                    repo_message,
                    import_result.files_scanned,
                    import_result.units_imported,
                    json.dumps(import_result.errors),
                ),
            )
            return {
                "game_system": config.id,
                "game_label": config.label,
                "status": status_text,
                "repo_message": repo_message,
                "repo_dir": str(bsdata_dir),
                "files_scanned": import_result.files_scanned,
                "units_imported": import_result.units_imported,
                "errors": import_result.errors,
            }
    except Exception as exc:
        finished_at = utc_now_sql()
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO import_runs (
                    game_system, started_at, finished_at, status, message, repo_message,
                    files_scanned, units_imported, errors_json
                ) VALUES (?, ?, ?, 'failed', ?, ?, 0, 0, ?)
                """,
                (config.id, started_at, finished_at, str(exc), repo_message, json.dumps([str(exc)])),
            )
        raise HTTPException(status_code=500, detail=f"{config.label} BSData sync failed: {exc}") from exc


@app.post("/api/sync")
def sync_bsdata(game_system: str = Query(default=DEFAULT_GAME_SYSTEM)) -> dict[str, Any]:
    return _sync_game_system(_config_or_400(game_system))


@app.post("/api/sync/{game_system}")
def sync_bsdata_for_system(game_system: str) -> dict[str, Any]:
    return _sync_game_system(_config_or_400(game_system))


@app.get("/api/factions")
def factions(game_system: str = Query(default=DEFAULT_GAME_SYSTEM)) -> list[dict[str, Any]]:
    config = _config_or_400(game_system)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT faction, COUNT(*) AS unit_count
            FROM bsd_units
            WHERE game_system = ? AND active = 1
            GROUP BY faction
            ORDER BY faction COLLATE NOCASE
            """,
            (config.id,),
        ).fetchall()
        return [dict(row) for row in rows]


@app.get("/api/units")
def units(
    game_system: str = Query(default=DEFAULT_GAME_SYSTEM),
    query: str | None = Query(default=None, max_length=100),
    faction: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    config = _config_or_400(game_system)
    sql = [
        """
        SELECT id, game_system, bs_id, name, faction, catalogue_file, entry_type, points,
               keywords, stats_json, wargear_options_json, imported_at
        FROM bsd_units
        WHERE game_system = ? AND active = 1
        """
    ]
    params: list[Any] = [config.id]

    cleaned_query = _clean_optional(query)
    if cleaned_query:
        # Tokenised search makes plural/simple variants work better, e.g.
        # "chaos lords" will still match "Chaos Lord".
        for token in cleaned_query.split():
            variants = {token}
            lower = token.lower()
            if len(token) > 3 and lower.endswith("s"):
                variants.add(token[:-1])
            if len(token) > 4 and lower.endswith("ies"):
                variants.add(token[:-3] + "y")
            conditions: list[str] = []
            for variant in sorted(variants):
                like = f"%{variant}%"
                for column in ("name", "faction", "keywords", "entry_type", "catalogue_file"):
                    conditions.append(f"{column} LIKE ?")
                    params.append(like)
            sql.append("AND (" + " OR ".join(conditions) + ")")

    cleaned_faction = _clean_optional(faction)
    if cleaned_faction:
        sql.append("AND faction = ?")
        params.append(cleaned_faction)

    sql.append("ORDER BY faction COLLATE NOCASE, name COLLATE NOCASE LIMIT ?")
    params.append(limit)

    with connect() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()
        return [_decode_stats(dict(row)) for row in rows]


@app.get("/api/inventory")
def inventory(game_system: str = Query(default=DEFAULT_GAME_SYSTEM)) -> list[dict[str, Any]]:
    config = _config_or_400(game_system)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                i.*,
                u.points AS current_points,
                u.keywords AS current_keywords,
                u.stats_json AS current_stats_json,
                u.wargear_options_json AS current_wargear_options_json,
                u.active AS unit_active
            FROM inventory_items i
            LEFT JOIN bsd_units u ON u.id = i.unit_id
            WHERE i.game_system = ?
            ORDER BY COALESCE(i.faction, ''), i.unit_name COLLATE NOCASE, i.id
            """,
            (config.id,),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            _ensure_inventory_copies(conn, row)
            item = _inventory_dict(row)
            stats_json = item.pop("current_stats_json", None)
            try:
                item["current_stats"] = json.loads(stats_json) if stats_json else {}
            except json.JSONDecodeError:
                item["current_stats"] = {}
            output.append(item)
        _attach_copies(conn, output)
        _attach_images(conn, output)
        return output


@app.post("/api/inventory", status_code=201)
def create_inventory_item(payload: InventoryPayload) -> dict[str, Any]:
    now = utc_now_sql()
    with connect() as conn:
        data = _payload_with_unit_snapshot(payload, conn)
        cursor = conn.execute(
            """
            INSERT INTO inventory_items (
                game_system, unit_id, unit_name, faction, catalogue_file, quantity, models_owned,
                built_count, painted_count, wargear, wargear_selections_json, model_number, storage_location, notes, acquired_on,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["game_system"],
                data["unit_id"],
                data["unit_name"],
                data["faction"],
                data["catalogue_file"],
                data["quantity"],
                data["models_owned"],
                data["built_count"],
                data["painted_count"],
                data["wargear"],
                data["wargear_selections_json"],
                data["model_number"],
                data["storage_location"],
                data["notes"],
                data["acquired_on"],
                now,
                now,
            ),
        )
        item_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
        _ensure_inventory_copies(conn, row)
        return _inventory_item_response(conn, item_id)


@app.put("/api/inventory/{item_id}")
def update_inventory_item(item_id: int, payload: InventoryPayload) -> dict[str, Any]:
    now = utc_now_sql()
    image_rows_to_delete: list[Any] = []
    with connect() as conn:
        existing = conn.execute("SELECT id FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Inventory item not found.")

        data = _payload_with_unit_snapshot(payload, conn)
        conn.execute(
            """
            UPDATE inventory_items SET
                game_system = ?,
                unit_id = ?,
                unit_name = ?,
                faction = ?,
                catalogue_file = ?,
                quantity = ?,
                models_owned = ?,
                built_count = ?,
                painted_count = ?,
                wargear = ?,
                wargear_selections_json = ?,
                model_number = ?,
                storage_location = ?,
                notes = ?,
                acquired_on = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                data["game_system"],
                data["unit_id"],
                data["unit_name"],
                data["faction"],
                data["catalogue_file"],
                data["quantity"],
                data["models_owned"],
                data["built_count"],
                data["painted_count"],
                data["wargear"],
                data["wargear_selections_json"],
                data["model_number"],
                data["storage_location"],
                data["notes"],
                data["acquired_on"],
                now,
                item_id,
            ),
        )
        image_rows_to_delete = _trim_inventory_copies(conn, item_id, data["quantity"])
        row = conn.execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
        _ensure_inventory_copies(conn, row)
        item = _inventory_item_response(conn, item_id)

    for row in image_rows_to_delete:
        _delete_image_file(int(row["inventory_item_id"]), row["file_name"])
    return item


@app.put("/api/inventory/{item_id}/copies/{copy_id}")
def update_inventory_copy(item_id: int, copy_id: int, payload: InventoryCopyPayload) -> dict[str, Any]:
    now = utc_now_sql()
    with connect() as conn:
        copy_row = conn.execute(
            """
            SELECT c.*
            FROM inventory_copies c
            JOIN inventory_items i ON i.id = c.inventory_item_id
            WHERE c.id = ? AND c.inventory_item_id = ?
            """,
            (copy_id, item_id),
        ).fetchone()
        if copy_row is None:
            raise HTTPException(status_code=404, detail="Inventory copy not found.")

        data = _copy_payload_data(payload, _item_wargear_options(conn, item_id))
        conn.execute(
            """
            UPDATE inventory_copies SET
                model_number = ?,
                wargear = ?,
                wargear_selections_json = ?,
                storage_location = ?,
                notes = ?,
                updated_at = ?
            WHERE id = ? AND inventory_item_id = ?
            """,
            (
                data["model_number"],
                data["wargear"],
                data["wargear_selections_json"],
                data["storage_location"],
                data["notes"],
                now,
                copy_id,
                item_id,
            ),
        )
        row = conn.execute("SELECT * FROM inventory_copies WHERE id = ?", (copy_id,)).fetchone()
        copy = _inventory_copy_dict(row)
        parent = {"id": item_id, "copies": [copy], "images": []}
        _attach_images(conn, [parent])
        return copy


@app.delete("/api/inventory/{item_id}", status_code=204)
def delete_inventory_item(item_id: int) -> Response:
    with connect() as conn:
        image_rows = conn.execute(
            "SELECT inventory_item_id, file_name FROM inventory_images WHERE inventory_item_id = ?",
            (item_id,),
        ).fetchall()
        cursor = conn.execute("DELETE FROM inventory_items WHERE id = ?", (item_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Inventory item not found.")

    for row in image_rows:
        _delete_image_file(int(row["inventory_item_id"]), row["file_name"])
    return Response(status_code=204)


async def _store_inventory_image(
    item_id: int,
    image: UploadFile,
    image_role: str,
    caption: str | None = None,
    copy_id: int | None = None,
) -> dict[str, Any]:
    with connect() as conn:
        item = conn.execute("SELECT id FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="Inventory item not found.")
        if copy_id is not None:
            copy = conn.execute(
                "SELECT id FROM inventory_copies WHERE id = ? AND inventory_item_id = ?",
                (copy_id, item_id),
            ).fetchone()
            if copy is None:
                raise HTTPException(status_code=404, detail="Inventory copy not found.")

    ext = _safe_image_ext(image)
    content = await image.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image is too large. Maximum size is 12 MB.")
    if not content:
        raise HTTPException(status_code=400, detail="Image upload was empty.")

    item_dir = UPLOAD_DIR / "inventory" / str(item_id)
    item_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{uuid.uuid4().hex}{ext}"
    target_path = item_dir / file_name
    target_path.write_bytes(content)

    now = utc_now_sql()
    try:
        with connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO inventory_images (
                    inventory_item_id, inventory_copy_id, file_name, original_name, content_type,
                    image_role, caption, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    copy_id,
                    file_name,
                    _original_name(image.filename),
                    (image.content_type or "").split(";", 1)[0].lower() or None,
                    _safe_image_role(image_role),
                    _clean_textarea(caption),
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM inventory_images WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return _image_dict(row)
    except Exception:
        _delete_image_file(item_id, file_name)
        raise


@app.post("/api/inventory/{item_id}/images", status_code=201)
async def upload_inventory_image(
    item_id: int,
    image: UploadFile = File(...),
    image_role: str = Form(default="other"),
    caption: str | None = Form(default=None),
) -> dict[str, Any]:
    return await _store_inventory_image(item_id, image, image_role, caption)


@app.post("/api/inventory/{item_id}/copies/{copy_id}/images", status_code=201)
async def upload_inventory_copy_image(
    item_id: int,
    copy_id: int,
    image: UploadFile = File(...),
    image_role: str = Form(default="other"),
    caption: str | None = Form(default=None),
) -> dict[str, Any]:
    return await _store_inventory_image(item_id, image, image_role, caption, copy_id)


@app.delete("/api/images/{image_id}", status_code=204)
def delete_inventory_image(image_id: int) -> Response:
    with connect() as conn:
        row = conn.execute("SELECT * FROM inventory_images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Image not found.")
        conn.execute("DELETE FROM inventory_images WHERE id = ?", (image_id,))

    _delete_image_file(int(row["inventory_item_id"]), row["file_name"])
    return Response(status_code=204)


@app.get("/api/export.csv")
def export_inventory_csv(game_system: str = Query(default=DEFAULT_GAME_SYSTEM)) -> Response:
    config = _config_or_400(game_system)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                i.id,
                i.game_system,
                i.unit_name,
                i.faction,
                i.catalogue_file,
                i.quantity,
                i.models_owned,
                i.built_count,
                i.painted_count,
                MAX(i.models_owned - i.built_count, 0) AS unbuilt_count,
                MAX(i.models_owned - i.painted_count, 0) AS unpainted_count,
                i.wargear,
                i.wargear_selections_json AS wargear_selections,
                i.model_number,
                i.storage_location,
                i.acquired_on,
                i.notes,
                u.points AS current_points,
                u.keywords AS current_keywords,
                u.wargear_options_json AS current_wargear_options_json,
                (SELECT COUNT(*) FROM inventory_images img WHERE img.inventory_item_id = i.id) AS image_count,
                i.created_at,
                i.updated_at
            FROM inventory_items i
            LEFT JOIN bsd_units u ON u.id = i.unit_id
            WHERE i.game_system = ?
            ORDER BY i.unit_name COLLATE NOCASE
            """,
            (config.id,),
        ).fetchall()

    fieldnames = [
        "id",
        "game_system",
        "unit_name",
        "faction",
        "catalogue_file",
        "quantity",
        "models_owned",
        "built_count",
        "painted_count",
        "unbuilt_count",
        "unpainted_count",
        "wargear",
        "wargear_selections",
        "model_number",
        "storage_location",
        "acquired_on",
        "notes",
        "current_points",
        "current_keywords",
        "image_count",
        "created_at",
        "updated_at",
    ]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        output_row = {key: dict(row).get(key) for key in fieldnames}
        output_row["wargear"] = _wargear_text(dict(row).get("wargear"))
        writer.writerow(output_row)

    filename = f"warhammer_inventory_{config.id}.csv"
    return Response(
        content=stream.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
