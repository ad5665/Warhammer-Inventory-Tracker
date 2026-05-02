from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
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
AUTH_COOKIE_NAME = "wh40k_session"
PASSWORD_HASH_ITERATIONS = 260_000
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
ALLOWED_IMAGE_ROLES = {"built", "painted", "wip", "reference", "other"}
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,40}$")


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


AUTH_ENABLED = _env_flag("WH40K_AUTH_ENABLED", False)
AUTH_COOKIE_SECURE = _env_flag("WH40K_COOKIE_SECURE", False)
AUTH_SESSION_DAYS = max(1, int(os.getenv("WH40K_SESSION_DAYS", "30") or 30))
INITIAL_ADMIN_USERNAME = os.getenv("WH40K_ADMIN_USERNAME", "admin").strip() or "admin"


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


def auth_enabled() -> bool:
    return AUTH_ENABLED


def _password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PASSWORD_HASH_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt}${digest}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt, expected = stored_hash.split("$", 3)
        iterations = int(iterations_text)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        iterations,
    ).hex()
    return hmac.compare_digest(digest, expected)


def _session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _clean_username(username: str | None) -> str:
    cleaned = (username or "").strip().lower()
    if not USERNAME_RE.fullmatch(cleaned):
        raise HTTPException(
            status_code=400,
            detail="Username must be 3-40 characters and use only letters, numbers, dots, hyphens, or underscores.",
        )
    return cleaned


def _password_error(password: str | None) -> str | None:
    if not password or len(password) < 10:
        return "Password must be at least 10 characters."
    if len(password) > 200:
        return "Password is too long."
    return None


def _assign_unowned_inventory_to_admin(conn: Any, admin_id: int) -> None:
    conn.execute(
        "UPDATE inventory_items SET owner_user_id = ? WHERE owner_user_id IS NULL",
        (admin_id,),
    )


def ensure_initial_admin_user() -> None:
    if not auth_enabled():
        return

    username = _clean_username(INITIAL_ADMIN_USERNAME)
    with connect() as conn:
        admin = conn.execute(
            "SELECT id FROM auth_users WHERE is_admin = 1 ORDER BY id LIMIT 1",
        ).fetchone()
        if admin is not None:
            _assign_unowned_inventory_to_admin(conn, int(admin["id"]))
            return

        temporary_password = secrets.token_urlsafe(18)
        now = utc_now_sql()
        existing = conn.execute(
            "SELECT id FROM auth_users WHERE username = ?",
            (username,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO auth_users (
                    username, password_hash, is_admin, must_change_password, created_at, updated_at
                ) VALUES (?, ?, 1, 1, ?, ?)
                """,
                (username, _password_hash(temporary_password), now, now),
            )
            admin_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        else:
            conn.execute(
                """
                UPDATE auth_users
                SET password_hash = ?, is_admin = 1, must_change_password = 1, updated_at = ?
                WHERE id = ?
                """,
                (_password_hash(temporary_password), now, existing["id"]),
            )
            admin_id = int(existing["id"])
        _assign_unowned_inventory_to_admin(conn, admin_id)

    message = (
        "WH40K_AUTH_ENABLED is true and no admin user existed. "
        f"Temporary admin credentials: username={username} password={temporary_password} "
        "Open /admin after logging in and set a new password."
    )
    print(message, flush=True)


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_initial_admin_user()
    yield


app = FastAPI(
    title="Warhammer Stock Tracker",
    description="Track owned Warhammer 40,000, Kill Team, and Age of Sigmar models using BSData catalogue imports and SQLite.",
    version="0.3.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _public_auth_path(path: str) -> bool:
    return path in {"/login", "/auth/login", "/logout", "/favicon.ico"}


def _request_target(request: Request) -> str:
    path = request.url.path
    return f"{path}?{request.url.query}" if request.url.query else path


def _login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(url=f"/login?next={quote(_request_target(request))}", status_code=303)


def _get_session_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        return None

    token_hash = _session_token_hash(token)
    now = int(time.time())
    with connect() as conn:
        row = conn.execute(
            """
            SELECT u.*
            FROM auth_sessions s
            JOIN auth_users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        session = conn.execute(
            "SELECT id, expires_at FROM auth_sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if session is None:
            return None
        if int(session["expires_at"]) <= now:
            conn.execute("DELETE FROM auth_sessions WHERE id = ?", (session["id"],))
            return None
        return dict(row) if row is not None else None


def _require_user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def _require_admin(request: Request) -> dict[str, Any]:
    user = _require_user(request)
    if not int(user.get("is_admin") or 0):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not auth_enabled():
        return await call_next(request)

    path = request.url.path
    if _public_auth_path(path):
        return await call_next(request)

    user = _get_session_user(request)
    request.state.user = user
    if user is None:
        if path.startswith("/api"):
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
        return _login_redirect(request)

    password_change_allowed = path in {"/admin", "/admin/password", "/auth/logout"}
    if int(user.get("must_change_password") or 0) and not password_change_allowed:
        if path.startswith("/api"):
            return JSONResponse({"detail": "Password change required."}, status_code=403)
        return RedirectResponse(url="/admin", status_code=303)

    return await call_next(request)


def _html_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(title)} - Warhammer Stock Tracker</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0c1117;
      --panel: #151d27;
      --panel-soft: #1c2633;
      --text: #eef3f8;
      --muted: #aab8c6;
      --border: #2c3b4c;
      --accent: #f2c14e;
      --danger: #f97066;
      --ok: #58d68d;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: linear-gradient(180deg, #0c1117 0%, #0f1720 100%);
      color: var(--text);
      display: grid;
      place-items: start center;
      padding: 32px 16px;
    }}
    main {{ width: min(880px, 100%); display: grid; gap: 18px; }}
    .panel {{
      background: rgba(21, 29, 39, 0.95);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 22px;
      box-shadow: 0 18px 50px rgba(0, 0, 0, 0.3);
    }}
    h1, h2 {{ margin: 0 0 12px; }}
    p {{ color: var(--muted); line-height: 1.5; }}
    form {{ display: grid; gap: 12px; }}
    label {{ display: grid; gap: 6px; color: var(--muted); font-size: 0.9rem; }}
    input {{
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 11px 12px;
      color: var(--text);
      background: #0f1620;
      font: inherit;
    }}
    input[type="checkbox"] {{ width: auto; }}
    button, .button-link {{
      border: 0;
      border-radius: 10px;
      padding: 11px 14px;
      color: #111820;
      background: var(--accent);
      font-weight: 800;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      justify-content: center;
    }}
    .button-row {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    .secondary {{ background: #d9e4ef; color: #111820; }}
    .message {{ padding: 10px 12px; border-radius: 10px; background: var(--panel-soft); }}
    .error {{ background: rgba(249, 112, 102, 0.18); border: 1px solid rgba(249, 112, 102, 0.45); }}
    .ok {{ background: rgba(88, 214, 141, 0.16); border: 1px solid rgba(88, 214, 141, 0.42); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; border-bottom: 1px solid var(--border); padding: 8px; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 0.78rem; text-transform: uppercase; }}
  </style>
</head>
<body>
  <main>{body}</main>
</body>
</html>
""")


def _message_html(message: str | None, kind: str = "ok") -> str:
    if not message:
        return ""
    return f'<div class="message {kind}">{_escape(message)}</div>'


def _login_page(next_url: str = "/", error: str | None = None) -> HTMLResponse:
    if not auth_enabled():
        return _html_page("Login", """
          <section class="panel">
            <h1>Authentication is disabled</h1>
            <p>This instance is currently running without login protection.</p>
            <a class="button-link" href="/">Open tracker</a>
          </section>
        """)
    return _html_page("Login", f"""
      <section class="panel">
        <h1>Warhammer Stock Tracker</h1>
        <p>Sign in to open the tracker.</p>
        {_message_html(error, "error")}
        <form method="post" action="/auth/login">
          <input type="hidden" name="next_url" value="{_escape(next_url or "/")}">
          <label>
            Username
            <input name="username" autocomplete="username" required autofocus>
          </label>
          <label>
            Password
            <input name="password" type="password" autocomplete="current-password" required>
          </label>
          <button type="submit">Sign in</button>
        </form>
      </section>
    """)


def _admin_page(user: dict[str, Any], message: str | None = None, error: str | None = None) -> HTMLResponse:
    with connect() as conn:
        users = conn.execute(
            """
            SELECT id, username, is_admin, must_change_password, created_at, last_login_at
            FROM auth_users
            ORDER BY username COLLATE NOCASE
            """
        ).fetchall()

    user_rows = "".join(
        f"""
        <tr>
          <td>{_escape(row["username"])}</td>
          <td>{"Admin" if int(row["is_admin"] or 0) else "User"}</td>
          <td>{"Password change required" if int(row["must_change_password"] or 0) else "Active"}</td>
          <td>{_escape(row["last_login_at"] or "Never")}</td>
        </tr>
        """
        for row in users
    )
    current_required = "" if int(user.get("must_change_password") or 0) else "required"
    current_password_field = "" if int(user.get("must_change_password") or 0) else f"""
      <label>
        Current password
        <input name="current_password" type="password" autocomplete="current-password" {current_required}>
      </label>
    """
    intro = "Set a permanent admin password before using the tracker." if int(user.get("must_change_password") or 0) else "Manage local users for this tracker."

    return _html_page("Admin", f"""
      <section class="panel">
        <div class="button-row" style="justify-content: space-between;">
          <div>
            <h1>Admin</h1>
            <p>{_escape(intro)}</p>
          </div>
          <form method="post" action="/auth/logout">
            <button class="secondary" type="submit">Sign out</button>
          </form>
        </div>
        {_message_html(message, "ok")}
        {_message_html(error, "error")}
      </section>

      <section class="panel">
        <h2>Set your password</h2>
        <form method="post" action="/admin/password">
          {current_password_field}
          <label>
            New password
            <input name="new_password" type="password" autocomplete="new-password" required>
          </label>
          <label>
            Confirm new password
            <input name="confirm_password" type="password" autocomplete="new-password" required>
          </label>
          <button type="submit">Update password</button>
        </form>
      </section>

      <section class="panel">
        <h2>Create user</h2>
        <form method="post" action="/admin/users">
          <label>
            Username
            <input name="username" required>
          </label>
          <label>
            Password
            <input name="password" type="password" autocomplete="new-password" required>
          </label>
          <label style="display: flex; gap: 8px; align-items: center;">
            <input name="is_admin" type="checkbox" value="1">
            Admin user
          </label>
          <button type="submit">Create user</button>
        </form>
      </section>

      <section class="panel">
        <h2>Users</h2>
        <table>
          <thead>
            <tr><th>Username</th><th>Role</th><th>Status</th><th>Last login</th></tr>
          </thead>
          <tbody>{user_rows or '<tr><td colspan="4">No users yet.</td></tr>'}</tbody>
        </table>
      </section>
    """)


def _safe_next_url(next_url: str | None) -> str:
    if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url


def _issue_session_response(user_id: int, redirect_to: str) -> RedirectResponse:
    token = secrets.token_urlsafe(32)
    now = utc_now_sql()
    expires_at = int(time.time()) + (AUTH_SESSION_DAYS * 24 * 60 * 60)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (_session_token_hash(token), user_id, now, expires_at),
        )
        conn.execute(
            "UPDATE auth_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
            (now, now, user_id),
        )

    response = RedirectResponse(url=redirect_to, status_code=303)
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=AUTH_SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite="lax",
    )
    return response


def _clear_session_response(request: Request, redirect_to: str = "/login") -> RedirectResponse:
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token:
        with connect() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_session_token_hash(token),))
    response = RedirectResponse(url=redirect_to, status_code=303)
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


@app.get("/login", include_in_schema=False)
def login(next: str = "/"):
    return _login_page(_safe_next_url(next))


@app.post("/auth/login", include_in_schema=False)
def login_submit(
    username: str = Form(...),
    password: str = Form(...),
    next_url: str = Form(default="/"),
):
    if not auth_enabled():
        return RedirectResponse(url="/", status_code=303)

    cleaned_username = username.strip().lower()
    with connect() as conn:
        user = conn.execute("SELECT * FROM auth_users WHERE username = ?", (cleaned_username,)).fetchone()
    if user is None or not _verify_password(password, user["password_hash"]):
        return _login_page(_safe_next_url(next_url), "Invalid username or password.")

    redirect_to = "/admin" if int(user["must_change_password"] or 0) else _safe_next_url(next_url)
    return _issue_session_response(int(user["id"]), redirect_to)


@app.get("/logout", include_in_schema=False)
def logout_get(request: Request) -> RedirectResponse:
    return _clear_session_response(request)


@app.post("/auth/logout", include_in_schema=False)
def logout_post(request: Request) -> RedirectResponse:
    return _clear_session_response(request)


@app.get("/admin", include_in_schema=False)
def admin(request: Request) -> HTMLResponse:
    if not auth_enabled():
        return _html_page("Admin", """
          <section class="panel">
            <h1>Authentication is disabled</h1>
            <p>Set <code>WH40K_AUTH_ENABLED=true</code> before starting the app to use the admin portal.</p>
          </section>
        """)
    return _admin_page(_require_admin(request))


@app.post("/admin/password", include_in_schema=False)
def admin_password(
    request: Request,
    current_password: str = Form(default=""),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> HTMLResponse:
    user = _require_admin(request)
    if new_password != confirm_password:
        return _admin_page(user, error="New password and confirmation do not match.")
    if error := _password_error(new_password):
        return _admin_page(user, error=error)
    if not int(user.get("must_change_password") or 0) and not _verify_password(current_password, user["password_hash"]):
        return _admin_page(user, error="Current password is incorrect.")

    now = utc_now_sql()
    with connect() as conn:
        conn.execute(
            """
            UPDATE auth_users
            SET password_hash = ?, must_change_password = 0, updated_at = ?
            WHERE id = ?
            """,
            (_password_hash(new_password), now, user["id"]),
        )
        updated = dict(conn.execute("SELECT * FROM auth_users WHERE id = ?", (user["id"],)).fetchone())
    request.state.user = updated
    return _admin_page(updated, message="Password updated.")


@app.post("/admin/users", include_in_schema=False)
def admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: str | None = Form(default=None),
) -> HTMLResponse:
    user = _require_admin(request)
    try:
        cleaned_username = _clean_username(username)
    except HTTPException as exc:
        return _admin_page(user, error=str(exc.detail))
    if error := _password_error(password):
        return _admin_page(user, error=error)

    now = utc_now_sql()
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO auth_users (
                    username, password_hash, is_admin, must_change_password, created_at, updated_at
                ) VALUES (?, ?, ?, 0, ?, ?)
                """,
                (cleaned_username, _password_hash(password), 1 if is_admin else 0, now, now),
            )
    except Exception:
        return _admin_page(user, error=f"Could not create user '{cleaned_username}'. The username may already exist.")
    return _admin_page(user, message=f"Created user '{cleaned_username}'.")


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    if not auth_enabled():
        return {"auth_enabled": False, "user": None}
    user = _require_user(request)
    return {
        "auth_enabled": True,
        "user": {
            "username": user["username"],
            "is_admin": bool(user["is_admin"]),
            "must_change_password": bool(user["must_change_password"]),
        },
    }


def _inventory_owner_id(request: Request) -> int | None:
    if not auth_enabled():
        return None
    return int(_require_user(request)["id"])


def _inventory_owner_clause(owner_user_id: int | None, alias: str = "i") -> tuple[str, list[Any]]:
    column = f"{alias}.owner_user_id" if alias else "owner_user_id"
    if owner_user_id is None:
        return f"{column} IS NULL", []
    return f"{column} = ?", [owner_user_id]


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
    return _wargear_options_from_payload(decoded)


def _wargear_options_from_payload(decoded: Any) -> list[dict[str, Any]]:
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


def _safe_optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _decode_model_composition(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []

    components: list[dict[str, Any]] = []
    for component in decoded:
        if not isinstance(component, dict):
            continue
        key = _clean_optional(str(component.get("key") or ""))
        name = _clean_optional(str(component.get("name") or ""))
        if not key or not name:
            continue
        options = _wargear_options_from_payload(component.get("wargear_options"))
        components.append({
            "key": key,
            "name": name,
            "min_models": _safe_optional_int(component.get("min_models")),
            "max_models": _safe_optional_int(component.get("max_models")),
            "wargear_options": options,
            "wargear_option_count": len(options),
        })
    return components


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
    row["model_composition"] = _decode_model_composition(row.pop("model_composition_json", None))
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


@app.get("/uploads/inventory/{item_id}/{file_name}", include_in_schema=False)
def uploaded_inventory_image(request: Request, item_id: int, file_name: str) -> FileResponse:
    safe_name = Path(file_name).name
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT img.file_name
            FROM inventory_images img
            JOIN inventory_items i ON i.id = img.inventory_item_id
            WHERE img.inventory_item_id = ? AND img.file_name = ? AND {owner_clause}
            """,
            [item_id, safe_name, *owner_params],
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Image not found.")

    path = _image_path(item_id, safe_name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Image not found.")
    return FileResponse(path)


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
    item.pop("owner_user_id", None)
    item["unbuilt_count"] = max(int(item.get("models_owned") or 0) - int(item.get("built_count") or 0), 0)
    item["unpainted_count"] = max(int(item.get("models_owned") or 0) - int(item.get("painted_count") or 0), 0)
    item["wargear_selections"] = _decode_wargear_selections(item.pop("wargear_selections_json", None))
    item["wargear_options"] = _decode_wargear_options(item.pop("current_wargear_options_json", None))
    item["model_composition"] = _decode_model_composition(item.pop("current_model_composition_json", None))
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


def _inventory_item_response(conn: Any, item_id: int, owner_user_id: int | None) -> dict[str, Any]:
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    row = conn.execute(
        f"""
        SELECT
            i.*,
            u.points AS current_points,
            u.min_models AS current_min_models,
            u.max_models AS current_max_models,
            u.keywords AS current_keywords,
            u.stats_json AS current_stats_json,
            u.wargear_options_json AS current_wargear_options_json,
            u.model_composition_json AS current_model_composition_json,
            u.active AS unit_active
        FROM inventory_items i
        LEFT JOIN bsd_units u ON u.id = i.unit_id
        WHERE i.id = ? AND {owner_clause}
        """,
        [item_id, *owner_params],
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


def _item_wargear_options(conn: Any, item_id: int, owner_user_id: int | None) -> list[dict[str, Any]]:
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    row = conn.execute(
        f"""
        SELECT u.wargear_options_json
        FROM inventory_items i
        LEFT JOIN bsd_units u ON u.id = i.unit_id
        WHERE i.id = ? AND {owner_clause}
        """,
        [item_id, *owner_params],
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
def status(request: Request, game_system: str = Query(default=DEFAULT_GAME_SYSTEM)) -> dict[str, Any]:
    config = _config_or_400(game_system)
    bsdata_dir = _bsdata_dir(config)
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
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
            f"SELECT COUNT(*) AS count FROM inventory_items i WHERE i.game_system = ? AND {owner_clause}",
            [config.id, *owner_params],
        ).fetchone()["count"]
        image_count = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM inventory_images img
            JOIN inventory_items i ON i.id = img.inventory_item_id
            WHERE i.game_system = ? AND {owner_clause}
            """,
            [config.id, *owner_params],
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
               min_models, max_models, keywords, stats_json, wargear_options_json,
               model_composition_json, imported_at
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
def inventory(request: Request, game_system: str = Query(default=DEFAULT_GAME_SYSTEM)) -> list[dict[str, Any]]:
    config = _config_or_400(game_system)
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                i.*,
                u.points AS current_points,
                u.min_models AS current_min_models,
                u.max_models AS current_max_models,
                u.keywords AS current_keywords,
                u.stats_json AS current_stats_json,
                u.wargear_options_json AS current_wargear_options_json,
                u.model_composition_json AS current_model_composition_json,
                u.active AS unit_active
            FROM inventory_items i
            LEFT JOIN bsd_units u ON u.id = i.unit_id
            WHERE i.game_system = ? AND {owner_clause}
            ORDER BY COALESCE(i.faction, ''), i.unit_name COLLATE NOCASE, i.id
            """,
            [config.id, *owner_params],
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
def create_inventory_item(request: Request, payload: InventoryPayload) -> dict[str, Any]:
    now = utc_now_sql()
    owner_user_id = _inventory_owner_id(request)
    with connect() as conn:
        data = _payload_with_unit_snapshot(payload, conn)
        cursor = conn.execute(
            """
            INSERT INTO inventory_items (
                owner_user_id, game_system, unit_id, unit_name, faction, catalogue_file, quantity, models_owned,
                built_count, painted_count, wargear, wargear_selections_json, model_number, storage_location, notes, acquired_on,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_user_id,
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
        return _inventory_item_response(conn, item_id, owner_user_id)


@app.put("/api/inventory/{item_id}")
def update_inventory_item(request: Request, item_id: int, payload: InventoryPayload) -> dict[str, Any]:
    now = utc_now_sql()
    image_rows_to_delete: list[Any] = []
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    owner_update_clause, owner_update_params = _inventory_owner_clause(owner_user_id, alias="")
    with connect() as conn:
        existing = conn.execute(
            f"SELECT id FROM inventory_items i WHERE i.id = ? AND {owner_clause}",
            [item_id, *owner_params],
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Inventory item not found.")

        data = _payload_with_unit_snapshot(payload, conn)
        conn.execute(
            f"""
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
            WHERE id = ? AND {owner_update_clause}
            """,
            [
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
                *owner_update_params,
            ],
        )
        image_rows_to_delete = _trim_inventory_copies(conn, item_id, data["quantity"])
        row = conn.execute(
            f"SELECT * FROM inventory_items i WHERE i.id = ? AND {owner_clause}",
            [item_id, *owner_params],
        ).fetchone()
        _ensure_inventory_copies(conn, row)
        item = _inventory_item_response(conn, item_id, owner_user_id)

    for row in image_rows_to_delete:
        _delete_image_file(int(row["inventory_item_id"]), row["file_name"])
    return item


@app.put("/api/inventory/{item_id}/copies/{copy_id}")
def update_inventory_copy(request: Request, item_id: int, copy_id: int, payload: InventoryCopyPayload) -> dict[str, Any]:
    now = utc_now_sql()
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    with connect() as conn:
        copy_row = conn.execute(
            f"""
            SELECT c.*
            FROM inventory_copies c
            JOIN inventory_items i ON i.id = c.inventory_item_id
            WHERE c.id = ? AND c.inventory_item_id = ? AND {owner_clause}
            """,
            [copy_id, item_id, *owner_params],
        ).fetchone()
        if copy_row is None:
            raise HTTPException(status_code=404, detail="Inventory copy not found.")

        data = _copy_payload_data(payload, _item_wargear_options(conn, item_id, owner_user_id))
        conn.execute(
            f"""
            UPDATE inventory_copies SET
                model_number = ?,
                wargear = ?,
                wargear_selections_json = ?,
                storage_location = ?,
                notes = ?,
                updated_at = ?
            WHERE id = ? AND inventory_item_id = ?
              AND EXISTS (
                SELECT 1
                FROM inventory_items i
                WHERE i.id = inventory_copies.inventory_item_id AND {owner_clause}
              )
            """,
            [
                data["model_number"],
                data["wargear"],
                data["wargear_selections_json"],
                data["storage_location"],
                data["notes"],
                now,
                copy_id,
                item_id,
                *owner_params,
            ],
        )
        row = conn.execute("SELECT * FROM inventory_copies WHERE id = ?", (copy_id,)).fetchone()
        copy = _inventory_copy_dict(row)
        parent = {"id": item_id, "copies": [copy], "images": []}
        _attach_images(conn, [parent])
        return copy


@app.delete("/api/inventory/{item_id}", status_code=204)
def delete_inventory_item(request: Request, item_id: int) -> Response:
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    owner_delete_clause, owner_delete_params = _inventory_owner_clause(owner_user_id, alias="")
    with connect() as conn:
        image_rows = conn.execute(
            f"""
            SELECT img.inventory_item_id, img.file_name
            FROM inventory_images img
            JOIN inventory_items i ON i.id = img.inventory_item_id
            WHERE img.inventory_item_id = ? AND {owner_clause}
            """,
            [item_id, *owner_params],
        ).fetchall()
        cursor = conn.execute(
            f"DELETE FROM inventory_items WHERE id = ? AND {owner_delete_clause}",
            [item_id, *owner_delete_params],
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Inventory item not found.")

    for row in image_rows:
        _delete_image_file(int(row["inventory_item_id"]), row["file_name"])
    return Response(status_code=204)


async def _store_inventory_image(
    owner_user_id: int | None,
    item_id: int,
    image: UploadFile,
    image_role: str,
    caption: str | None = None,
    copy_id: int | None = None,
) -> dict[str, Any]:
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    with connect() as conn:
        item = conn.execute(
            f"SELECT id FROM inventory_items i WHERE i.id = ? AND {owner_clause}",
            [item_id, *owner_params],
        ).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="Inventory item not found.")
        if copy_id is not None:
            copy = conn.execute(
                f"""
                SELECT c.id
                FROM inventory_copies c
                JOIN inventory_items i ON i.id = c.inventory_item_id
                WHERE c.id = ? AND c.inventory_item_id = ? AND {owner_clause}
                """,
                [copy_id, item_id, *owner_params],
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
    request: Request,
    item_id: int,
    image: UploadFile = File(...),
    image_role: str = Form(default="other"),
    caption: str | None = Form(default=None),
) -> dict[str, Any]:
    return await _store_inventory_image(_inventory_owner_id(request), item_id, image, image_role, caption)


@app.post("/api/inventory/{item_id}/copies/{copy_id}/images", status_code=201)
async def upload_inventory_copy_image(
    request: Request,
    item_id: int,
    copy_id: int,
    image: UploadFile = File(...),
    image_role: str = Form(default="other"),
    caption: str | None = Form(default=None),
) -> dict[str, Any]:
    return await _store_inventory_image(_inventory_owner_id(request), item_id, image, image_role, caption, copy_id)


@app.delete("/api/images/{image_id}", status_code=204)
def delete_inventory_image(request: Request, image_id: int) -> Response:
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT img.*
            FROM inventory_images img
            JOIN inventory_items i ON i.id = img.inventory_item_id
            WHERE img.id = ? AND {owner_clause}
            """,
            [image_id, *owner_params],
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Image not found.")
        conn.execute("DELETE FROM inventory_images WHERE id = ?", (image_id,))

    _delete_image_file(int(row["inventory_item_id"]), row["file_name"])
    return Response(status_code=204)


@app.get("/api/export.csv")
def export_inventory_csv(request: Request, game_system: str = Query(default=DEFAULT_GAME_SYSTEM)) -> Response:
    config = _config_or_400(game_system)
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    with connect() as conn:
        rows = conn.execute(
            f"""
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
                u.min_models AS current_min_models,
                u.max_models AS current_max_models,
                u.keywords AS current_keywords,
                u.wargear_options_json AS current_wargear_options_json,
                u.model_composition_json AS current_model_composition_json,
                (SELECT COUNT(*) FROM inventory_images img WHERE img.inventory_item_id = i.id) AS image_count,
                i.created_at,
                i.updated_at
            FROM inventory_items i
            LEFT JOIN bsd_units u ON u.id = i.unit_id
            WHERE i.game_system = ? AND {owner_clause}
            ORDER BY i.unit_name COLLATE NOCASE
            """,
            [config.id, *owner_params],
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
        "current_min_models",
        "current_max_models",
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
