from __future__ import annotations

import asyncio
import csv
import base64
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import jwt
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jwt import PyJWKClient, PyJWTError
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
from .db import (
    DATA_DIR,
    _new_public_id,
    connect,
    database_label,
    init_db,
    table_count,
    utc_now_sql,
)
from .storage import (
    ObjectNotFound,
    delete_object,
    get_object,
    put_object,
    storage_label,
)

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
BSDATA_ROOT = DATA_DIR / "bsdata"
LOGGER = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_CSV_IMPORT_BYTES = 2 * 1024 * 1024
AUTH_COOKIE_NAME = "wh40k_session"
PASSWORD_HASH_ITERATIONS = 260_000
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
ALLOWED_IMAGE_ROLES = {"built", "painted", "wip", "reference", "other"}
ALLOWED_THEMES = {
    "default",
    "ultramarines",
    "blood-angels",
    "dark-angels",
    "space-wolves",
    "imperial-fists",
    "white-scars",
    "raven-guard",
    "salamanders",
    "iron-hands",
    "crimson-fists",
    "black-templars",
    "night-lords",
    "world-eaters",
    "death-guard",
    "thousand-sons",
    "emperors-children",
    "iron-warriors",
    "word-bearers",
    "alpha-legion",
    "black-legion",
    "sons-of-horus",
}
INVENTORY_CSV_FIELDNAMES = [
    "id",
    "public_id",
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
    "version",
]
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,40}$")


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


AUTH_ENABLED = _env_flag("WH40K_AUTH_ENABLED", False)
BSDATA_AUTO_SYNC_ENABLED = _env_flag("WH40K_BSDATA_AUTO_SYNC_ENABLED", True)
AUTH_COOKIE_SECURE = _env_flag("WH40K_COOKIE_SECURE", False)
AUTH_SESSION_DAYS = max(1, int(os.getenv("WH40K_SESSION_DAYS", "30") or 30))
INITIAL_ADMIN_USERNAME = os.getenv("WH40K_ADMIN_USERNAME", "admin").strip() or "admin"
AUTH_PROVIDER = os.getenv("AUTH_PROVIDER", "local").strip().lower() or "local"
OIDC_AUTH_PROVIDERS = {"oidc", "keycloak"}
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
OIDC_ISSUER_URL = os.getenv(
    "OIDC_ISSUER_URL", "http://localhost:8081/realms/wh40k"
).rstrip("/")
OIDC_INTERNAL_ISSUER_URL = os.getenv(
    "OIDC_INTERNAL_ISSUER_URL", OIDC_ISSUER_URL
).rstrip("/")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "wh40k-web").strip() or "wh40k-web"
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "").strip()
OIDC_SCOPE = (
    os.getenv("OIDC_SCOPE", "openid email profile").strip() or "openid email profile"
)
OIDC_ADMIN_ROLE = os.getenv("OIDC_ADMIN_ROLE", "wh40k-admin").strip() or "wh40k-admin"
OIDC_STATE_COOKIE_NAME = "wh40k_oidc_state"
OIDC_STATE_TTL_SECONDS = 10 * 60
_JWK_CLIENTS: dict[str, PyJWKClient] = {}


def _csv_env_values(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [part.strip() for part in value.split(",") if part.strip()]


CORS_ALLOWED_ORIGINS = _csv_env_values("WH40K_CORS_ORIGINS")
_BSDATA_SYNC_LOCK = threading.Lock()


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
    models_owned: int | None = Field(default=None, ge=0)
    built_count: int | None = Field(default=None, ge=0)
    painted_count: int | None = Field(default=None, ge=0)
    model_number: str | None = None
    wargear: str | None = None
    wargear_selections: dict[str, int] | None = None
    storage_location: str | None = None
    notes: str | None = None


class UserPreferencesPayload(BaseModel):
    theme: str = Field(default="default", min_length=1, max_length=64)


def auth_enabled() -> bool:
    return AUTH_ENABLED


def oidc_auth_enabled() -> bool:
    return auth_enabled() and AUTH_PROVIDER in OIDC_AUTH_PROVIDERS


def local_auth_enabled() -> bool:
    return auth_enabled() and not oidc_auth_enabled()


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


def _clean_theme(theme: str | None) -> str:
    cleaned = (theme or "default").strip().lower()
    if cleaned not in ALLOWED_THEMES:
        raise HTTPException(status_code=400, detail="Unknown theme.")
    return cleaned


def _assign_unowned_inventory_to_admin(conn: Any, admin_id: int) -> None:
    conn.execute(
        "UPDATE inventory_items SET owner_user_id = %s WHERE owner_user_id IS NULL",
        (admin_id,),
    )


def ensure_initial_admin_user() -> None:
    if not local_auth_enabled():
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
            "SELECT id FROM auth_users WHERE username = %s",
            (username,),
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO auth_users (
                    username, password_hash, is_admin, must_change_password, created_at, updated_at
                ) VALUES (%s, %s, 1, 1, %s, %s)
                RETURNING id
                """,
                (username, _password_hash(temporary_password), now, now),
            )
            admin_id = int(cursor.fetchone()["id"])
        else:
            conn.execute(
                """
                UPDATE auth_users
                SET password_hash = %s, is_admin = 1, must_change_password = 1, updated_at = %s
                WHERE id = %s
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


def _static_asset_version() -> str:
    configured = os.getenv("WH40K_BUILD_VERSION") or os.getenv("WH40K_ASSET_VERSION")
    if configured:
        return configured[:64]

    digest = hashlib.sha256()
    for asset_name in ("styles.css", "app.js"):
        path = STATIC_DIR / asset_name
        digest.update(asset_name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


STATIC_ASSET_VERSION = _static_asset_version()


def _versioned_index_html() -> str:
    asset_version = quote(STATIC_ASSET_VERSION, safe="")
    content = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return content.replace(
        "/static/styles.css", f"/static/styles.css?v={asset_version}"
    ).replace("/static/app.js", f"/static/app.js?v={asset_version}")


def _seconds_until_next_midnight(now: datetime | None = None) -> float:
    current = now if now is not None else datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    tomorrow = current.date() + timedelta(days=1)
    next_midnight = datetime.combine(tomorrow, datetime_time.min, tzinfo=current.tzinfo)
    return max(1.0, (next_midnight - current).total_seconds())


def _sync_summary_text(result: dict[str, Any]) -> str:
    imported = sum(
        int(item.get("units_imported") or 0) for item in result.get("results", [])
    )
    scanned = sum(
        int(item.get("files_scanned") or 0) for item in result.get("results", [])
    )
    failures = result.get("failures", [])
    if failures:
        return f"BSData sync completed with {len(failures)} failure(s). Imported {imported} entries from {scanned} files."
    return f"BSData sync completed. Imported {imported} entries from {scanned} files."


async def _run_scheduled_bsdata_sync(reason: str) -> None:
    try:
        LOGGER.info("Starting %s BSData sync.", reason)
        result = await asyncio.to_thread(_sync_all_game_systems)
    except HTTPException as exc:
        if exc.status_code == 409:
            LOGGER.info(
                "Skipped %s BSData sync because another sync is already running.",
                reason,
            )
            return
        LOGGER.warning("%s BSData sync failed: %s", reason.capitalize(), exc.detail)
    except Exception:
        LOGGER.exception("%s BSData sync failed.", reason.capitalize())
    else:
        LOGGER.info("%s", _sync_summary_text(result))


async def _bsdata_auto_sync_loop() -> None:
    await _run_scheduled_bsdata_sync("startup")
    while True:
        await asyncio.sleep(_seconds_until_next_midnight())
        await _run_scheduled_bsdata_sync("midnight")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_initial_admin_user()
    sync_task: asyncio.Task[None] | None = None
    if BSDATA_AUTO_SYNC_ENABLED:
        sync_task = asyncio.create_task(_bsdata_auto_sync_loop())
    try:
        yield
    finally:
        if sync_task is not None:
            sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await sync_task


app = FastAPI(
    title="Warhammer Stock Tracker",
    description="Track owned Warhammer 40,000, Kill Team, and Age of Sigmar models using BSData catalogue imports.",
    version="0.4.0",
    lifespan=lifespan,
)
if CORS_ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    return HTMLResponse(
        _versioned_index_html(),
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


def _public_auth_path(path: str) -> bool:
    return path in {
        "/login",
        "/signup",
        "/auth/login",
        "/auth/oidc/login",
        "/auth/oidc/register",
        "/auth/oidc/callback",
        "/logout",
        "/favicon.ico",
    }


def _request_target(request: Request) -> str:
    path = request.url.path
    return f"{path}%s{request.url.query}" if request.url.query else path


def _login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(
        url=f"/login?next={quote(_request_target(request))}", status_code=303
    )


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
            WHERE s.token_hash = %s
            """,
            (token_hash,),
        ).fetchone()
        session = conn.execute(
            "SELECT id, expires_at FROM auth_sessions WHERE token_hash = %s",
            (token_hash,),
        ).fetchone()
        if session is None:
            return None
        if int(session["expires_at"]) <= now:
            conn.execute("DELETE FROM auth_sessions WHERE id = %s", (session["id"],))
            return None
        return dict(row) if row is not None else None


def _oidc_url(path: str, internal: bool = False) -> str:
    issuer = OIDC_INTERNAL_ISSUER_URL if internal else OIDC_ISSUER_URL
    return f"{issuer}{path}"


def _redirect_uri(request: Request) -> str:
    return f"{APP_PUBLIC_URL}/auth/oidc/callback"


def _oidc_jwks_client() -> PyJWKClient:
    jwks_url = _oidc_url("/protocol/openid-connect/certs", internal=True)
    if jwks_url not in _JWK_CLIENTS:
        _JWK_CLIENTS[jwks_url] = PyJWKClient(jwks_url)
    return _JWK_CLIENTS[jwks_url]


def _decode_oidc_token(token: str, *, verify_audience: bool = True) -> dict[str, Any]:
    try:
        signing_key = _oidc_jwks_client().get_signing_key_from_jwt(token)
        options = {} if verify_audience else {"verify_aud": False}
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512"],
            audience=OIDC_CLIENT_ID if verify_audience else None,
            issuer=OIDC_ISSUER_URL,
            options=options,
        )
    except PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid identity token.") from exc

    if not verify_audience:
        audiences = claims.get("aud") or []
        if isinstance(audiences, str):
            audiences = [audiences]
        authorized_party = claims.get("azp") or claims.get("client_id")
        if OIDC_CLIENT_ID not in audiences and authorized_party != OIDC_CLIENT_ID:
            raise HTTPException(
                status_code=401, detail="Token was not issued for this client."
            )
    return claims


def _pack_oidc_state(data: dict[str, str]) -> str:
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _unpack_oidc_state(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        data = json.loads(
            base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")
        )
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _oidc_role_names(claims: dict[str, Any]) -> set[str]:
    roles: set[str] = set()
    realm_access = claims.get("realm_access")
    if isinstance(realm_access, dict) and isinstance(realm_access.get("roles"), list):
        roles.update(str(role) for role in realm_access["roles"])

    resource_access = claims.get("resource_access")
    if isinstance(resource_access, dict):
        for client_access in resource_access.values():
            if isinstance(client_access, dict) and isinstance(
                client_access.get("roles"), list
            ):
                roles.update(str(role) for role in client_access["roles"])
    return roles


def _merge_oidc_role_claims(
    primary: dict[str, Any], secondary: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(primary)
    roles = _oidc_role_names(primary) | _oidc_role_names(secondary)
    if not roles:
        return merged

    realm_access = merged.get("realm_access")
    merged_realm_access = dict(realm_access) if isinstance(realm_access, dict) else {}
    existing_roles = merged_realm_access.get("roles")
    if isinstance(existing_roles, list):
        roles.update(str(role) for role in existing_roles)
    merged_realm_access["roles"] = sorted(roles)
    merged["realm_access"] = merged_realm_access
    return merged


def _oidc_user_claims(id_token: str | None, access_token: str | None) -> dict[str, Any]:
    if id_token:
        claims = _decode_oidc_token(id_token, verify_audience=True)
        if access_token:
            access_claims = _decode_oidc_token(access_token, verify_audience=False)
            return _merge_oidc_role_claims(claims, access_claims)
        return claims
    if access_token:
        return _decode_oidc_token(access_token, verify_audience=False)
    raise HTTPException(
        status_code=401, detail="The identity provider did not return a token."
    )


def _oidc_username(claims: dict[str, Any]) -> str:
    base = (
        claims.get("preferred_username")
        or claims.get("email")
        or claims.get("sub")
        or "user"
    )
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", ".", str(base).strip().lower()).strip("._-")
    if len(cleaned) < 3:
        cleaned = f"user-{secrets.token_hex(4)}"
    return cleaned[:40]


def _unique_provider_username(conn: Any, username: str) -> str:
    root = username[:34].rstrip("._-") or "user"
    candidate = root
    suffix = 2
    while (
        conn.execute(
            "SELECT id FROM auth_users WHERE username = %s", (candidate,)
        ).fetchone()
        is not None
    ):
        candidate = f"{root[:34]}-{suffix}"
        suffix += 1
    return candidate


def _upsert_oidc_user(claims: dict[str, Any]) -> dict[str, Any]:
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise HTTPException(
            status_code=401, detail="Identity token is missing a subject."
        )

    now = utc_now_sql()
    email = str(claims.get("email") or "").strip().lower() or None
    display_name = (
        str(claims.get("name") or claims.get("preferred_username") or "").strip()
        or None
    )
    is_admin = 1 if OIDC_ADMIN_ROLE in _oidc_role_names(claims) else 0

    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM auth_users WHERE auth_provider = %s AND auth_subject = %s",
            (AUTH_PROVIDER, subject),
        ).fetchone()
        if existing is None:
            username = _unique_provider_username(conn, _oidc_username(claims))
            cursor = conn.execute(
                """
                INSERT INTO auth_users (
                    username, password_hash, auth_provider, auth_subject, email, display_name,
                    is_admin, must_change_password, created_at, updated_at, last_login_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s)
                RETURNING id
                """,
                (
                    username,
                    "provider-managed",
                    AUTH_PROVIDER,
                    subject,
                    email,
                    display_name,
                    is_admin,
                    now,
                    now,
                    now,
                ),
            )
            user_id = int(cursor.fetchone()["id"])
        else:
            user_id = int(existing["id"])
            conn.execute(
                """
                UPDATE auth_users
                SET email = %s,
                    display_name = %s,
                    is_admin = %s,
                    updated_at = %s,
                    last_login_at = %s
                WHERE id = %s
                """,
                (email, display_name, is_admin, now, now, user_id),
            )
        return dict(
            conn.execute(
                "SELECT * FROM auth_users WHERE id = %s", (user_id,)
            ).fetchone()
        )


def _get_bearer_user(request: Request) -> dict[str, Any] | None:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return _upsert_oidc_user(_decode_oidc_token(token.strip(), verify_audience=False))


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

    try:
        user = _get_bearer_user(request) if oidc_auth_enabled() else None
    except HTTPException as exc:
        if path.startswith("/api"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return _login_redirect(request)
    if user is None:
        user = _get_session_user(request)
    request.state.user = user
    if user is None:
        if path.startswith("/api"):
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
        return _login_redirect(request)

    password_change_allowed = path in {"/admin", "/admin/password", "/auth/logout"}
    if int(user.get("must_change_password") or 0) and not password_change_allowed:
        if path.startswith("/api"):
            return JSONResponse(
                {"detail": "Password change required."}, status_code=403
            )
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


def _bsdata_admin_panel() -> str:
    return """
      <section class="panel">
        <h2>BSData catalogue</h2>
        <p>Catalogue data syncs automatically when the app starts and again at local midnight.</p>
        <form method="post" action="/admin/bsdata/sync">
          <button type="submit">Sync BSData</button>
        </form>
      </section>
    """


def _login_page(next_url: str = "/", error: str | None = None) -> HTMLResponse:
    if not auth_enabled():
        return _html_page(
            "Login",
            """
          <section class="panel">
            <h1>Authentication is disabled</h1>
            <p>This instance is currently running without login protection.</p>
            <a class="button-link" href="/">Open tracker</a>
          </section>
        """,
        )
    if oidc_auth_enabled():
        encoded_next = quote(_safe_next_url(next_url), safe="")
        return _html_page(
            "Login",
            f"""
          <section class="panel">
            <h1>Warhammer Stock Tracker</h1>
            <p>Sign in with the identity provider to open the tracker.</p>
            {_message_html(error, "error")}
            <div class="button-row">
              <a class="button-link" href="/auth/oidc/login?next={encoded_next}">Sign in</a>
              <a class="button-link secondary" href="/signup?next={encoded_next}">Create account</a>
            </div>
          </section>
        """,
        )
    return _html_page(
        "Login",
        f"""
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
    """,
    )


def _signup_page(next_url: str = "/", error: str | None = None) -> HTMLResponse:
    if not oidc_auth_enabled():
        return _html_page(
            "Sign Up",
            """
          <section class="panel">
            <h1>Self-service signup is not enabled</h1>
            <p>Set <code>AUTH_PROVIDER=keycloak</code> and <code>WH40K_AUTH_ENABLED=true</code> to use provider signup.</p>
            <a class="button-link" href="/login">Back to sign in</a>
          </section>
        """,
        )
    encoded_next = quote(_safe_next_url(next_url), safe="")
    return _html_page(
        "Sign Up",
        f"""
      <section class="panel">
        <h1>Create account</h1>
        <p>Accounts are created by Keycloak. After signup, you will return to the tracker with your own private inventory.</p>
        {_message_html(error, "error")}
        <div class="button-row">
          <a class="button-link" href="/auth/oidc/register?next={encoded_next}">Create account</a>
          <a class="button-link secondary" href="/login?next={encoded_next}">Sign in instead</a>
        </div>
      </section>
    """,
    )


def _admin_page(
    user: dict[str, Any], message: str | None = None, error: str | None = None
) -> HTMLResponse:
    with connect() as conn:
        users = conn.execute(
            """
            SELECT id, username, is_admin, must_change_password, created_at, last_login_at
            FROM auth_users
            ORDER BY lower(username)
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
    current_password_field = (
        ""
        if int(user.get("must_change_password") or 0)
        else f"""
      <label>
        Current password
        <input name="current_password" type="password" autocomplete="current-password" {current_required}>
      </label>
    """
    )
    intro = (
        "Set a permanent admin password before using the tracker."
        if int(user.get("must_change_password") or 0)
        else "Manage local users for this tracker."
    )

    return _html_page(
        "Admin",
        f"""
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

      {_bsdata_admin_panel()}

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
    """,
    )


def _provider_admin_page(
    user: dict[str, Any],
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    admin_url = os.getenv(
        "OIDC_ADMIN_URL", "http://localhost:8081/admin/master/console/#/wh40k"
    ).strip()
    return _html_page(
        "Admin",
        f"""
      <section class="panel">
        <div class="button-row" style="justify-content: space-between;">
          <div>
            <h1>Admin</h1>
            <p>Users, passwords, and signup settings are managed by the identity provider.</p>
          </div>
          <form method="post" action="/auth/logout">
            <button class="secondary" type="submit">Sign out</button>
          </form>
        </div>
        {_message_html(message, "ok")}
        {_message_html(error, "error")}
      </section>
      {_bsdata_admin_panel()}
      <section class="panel">
        <h2>Current user</h2>
        <p>{_escape(user.get("display_name") or user.get("username"))}</p>
        <div class="button-row">
          <a class="button-link" href="{_escape(admin_url)}">Open Keycloak admin</a>
          <a class="button-link secondary" href="/">Open tracker</a>
        </div>
      </section>
    """,
    )


def _safe_next_url(next_url: str | None) -> str:
    if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url


def _oidc_redirect(
    request: Request, next_url: str, endpoint: str = "auth"
) -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    safe_next = _safe_next_url(next_url)
    payload = _pack_oidc_state(
        {
            "state": state,
            "next": safe_next,
            "verifier": verifier,
        }
    )
    params = {
        "client_id": OIDC_CLIENT_ID,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": OIDC_SCOPE,
        "state": state,
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(
        url=f"{_oidc_url(f'/protocol/openid-connect/{endpoint}')}%s{urlencode(params)}",
        status_code=303,
    )
    response.set_cookie(
        OIDC_STATE_COOKIE_NAME,
        payload,
        max_age=OIDC_STATE_TTL_SECONDS,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite="lax",
    )
    return response


def _provider_logout_url() -> str:
    params = {
        "client_id": OIDC_CLIENT_ID,
        "post_logout_redirect_uri": f"{APP_PUBLIC_URL}/login",
    }
    return f"{_oidc_url('/protocol/openid-connect/logout')}%s{urlencode(params)}"


def _issue_session_response(user_id: int, redirect_to: str) -> RedirectResponse:
    token = secrets.token_urlsafe(32)
    now = utc_now_sql()
    expires_at = int(time.time()) + (AUTH_SESSION_DAYS * 24 * 60 * 60)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at)
            VALUES (%s, %s, %s, %s)
            """,
            (_session_token_hash(token), user_id, now, expires_at),
        )
        conn.execute(
            "UPDATE auth_users SET last_login_at = %s, updated_at = %s WHERE id = %s",
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


def _clear_session_response(
    request: Request, redirect_to: str = "/login"
) -> RedirectResponse:
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token:
        with connect() as conn:
            conn.execute(
                "DELETE FROM auth_sessions WHERE token_hash = %s",
                (_session_token_hash(token),),
            )
    response = RedirectResponse(
        url=_provider_logout_url() if oidc_auth_enabled() else redirect_to,
        status_code=303,
    )
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


@app.get("/login", include_in_schema=False)
def login(next: str = "/"):
    return _login_page(_safe_next_url(next))


@app.get("/signup", include_in_schema=False)
def signup(next: str = "/"):
    return _signup_page(_safe_next_url(next))


@app.post("/auth/login", include_in_schema=False)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_url: str = Form(default="/"),
):
    if not auth_enabled():
        return RedirectResponse(url="/", status_code=303)
    if oidc_auth_enabled():
        return _oidc_redirect(request, next_url)

    cleaned_username = username.strip().lower()
    with connect() as conn:
        user = conn.execute(
            "SELECT * FROM auth_users WHERE username = %s", (cleaned_username,)
        ).fetchone()
    if user is None or not _verify_password(password, user["password_hash"]):
        return _login_page(_safe_next_url(next_url), "Invalid username or password.")

    redirect_to = (
        "/admin" if int(user["must_change_password"] or 0) else _safe_next_url(next_url)
    )
    return _issue_session_response(int(user["id"]), redirect_to)


@app.get("/auth/oidc/login", include_in_schema=False)
def oidc_login(request: Request, next: str = "/") -> RedirectResponse:
    if not oidc_auth_enabled():
        return RedirectResponse(url="/login", status_code=303)
    return _oidc_redirect(request, next, "auth")


@app.get("/auth/oidc/register", include_in_schema=False)
def oidc_register(request: Request, next: str = "/") -> RedirectResponse:
    if not oidc_auth_enabled():
        return RedirectResponse(url="/signup", status_code=303)
    return _oidc_redirect(request, next, "registrations")


@app.get("/auth/oidc/callback", include_in_schema=False)
def oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    if not oidc_auth_enabled():
        return RedirectResponse(url="/login", status_code=303)
    if error:
        return _login_page("/", error_description or error)
    if not code or not state:
        return _login_page("/", "The identity provider did not return a login code.")

    stored = _unpack_oidc_state(request.cookies.get(OIDC_STATE_COOKIE_NAME))
    if not stored or not secrets.compare_digest(stored.get("state", ""), state):
        return _login_page("/", "The login request expired. Try signing in again.")

    data = {
        "grant_type": "authorization_code",
        "client_id": OIDC_CLIENT_ID,
        "code": code,
        "redirect_uri": _redirect_uri(request),
        "code_verifier": stored.get("verifier", ""),
    }
    if OIDC_CLIENT_SECRET:
        data["client_secret"] = OIDC_CLIENT_SECRET

    try:
        token_response = httpx.post(
            _oidc_url("/protocol/openid-connect/token", internal=True),
            data=data,
            timeout=15,
        )
        token_response.raise_for_status()
    except httpx.HTTPError as exc:
        return _login_page("/", f"Could not complete provider login: {exc}")

    token_payload = token_response.json()
    id_token = token_payload.get("id_token")
    access_token = token_payload.get("access_token")
    if not id_token and not access_token:
        return _login_page("/", "The identity provider did not return a token.")

    claims = _oidc_user_claims(id_token, access_token)
    user = _upsert_oidc_user(claims)
    response = _issue_session_response(
        int(user["id"]), _safe_next_url(stored.get("next"))
    )
    response.delete_cookie(OIDC_STATE_COOKIE_NAME)
    return response


@app.get("/logout", include_in_schema=False)
def logout_get(request: Request) -> RedirectResponse:
    return _clear_session_response(request)


@app.post("/auth/logout", include_in_schema=False)
def logout_post(request: Request) -> RedirectResponse:
    return _clear_session_response(request)


@app.get("/admin", include_in_schema=False)
def admin(request: Request) -> HTMLResponse:
    if not auth_enabled():
        return _html_page(
            "Admin",
            """
          <section class="panel">
            <h1>Authentication is disabled</h1>
            <p>Set <code>WH40K_AUTH_ENABLED=true</code> before starting the app to use the admin portal.</p>
          </section>
        """,
        )
    if oidc_auth_enabled():
        return _provider_admin_page(_require_admin(request))
    return _admin_page(_require_admin(request))


@app.post("/admin/password", include_in_schema=False)
def admin_password(
    request: Request,
    current_password: str = Form(default=""),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> HTMLResponse:
    user = _require_admin(request)
    if oidc_auth_enabled():
        return _provider_admin_page(user)
    if new_password != confirm_password:
        return _admin_page(user, error="New password and confirmation do not match.")
    if error := _password_error(new_password):
        return _admin_page(user, error=error)
    if not int(user.get("must_change_password") or 0) and not _verify_password(
        current_password, user["password_hash"]
    ):
        return _admin_page(user, error="Current password is incorrect.")

    now = utc_now_sql()
    with connect() as conn:
        conn.execute(
            """
            UPDATE auth_users
            SET password_hash = %s, must_change_password = 0, updated_at = %s
            WHERE id = %s
            """,
            (_password_hash(new_password), now, user["id"]),
        )
        updated = dict(
            conn.execute(
                "SELECT * FROM auth_users WHERE id = %s", (user["id"],)
            ).fetchone()
        )
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
    if oidc_auth_enabled():
        return _provider_admin_page(user)
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
                ) VALUES (%s, %s, %s, 0, %s, %s)
                """,
                (
                    cleaned_username,
                    _password_hash(password),
                    1 if is_admin else 0,
                    now,
                    now,
                ),
            )
    except Exception:
        return _admin_page(
            user,
            error=f"Could not create user '{cleaned_username}'. The username may already exist.",
        )
    return _admin_page(user, message=f"Created user '{cleaned_username}'.")


@app.post("/admin/bsdata/sync", include_in_schema=False)
def admin_sync_bsdata(request: Request) -> HTMLResponse:
    user = _require_admin(request)
    try:
        result = _sync_all_game_systems()
    except HTTPException as exc:
        message = None
        error = str(exc.detail)
    else:
        summary = _sync_summary_text(result)
        message = None if result.get("failures") else summary
        error = summary if result.get("failures") else None
    if oidc_auth_enabled():
        return _provider_admin_page(user, message=message, error=error)
    return _admin_page(user, message=message, error=error)


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    if not auth_enabled():
        return {"auth_enabled": False, "provider": AUTH_PROVIDER, "user": None}
    user = _require_user(request)
    return {
        "auth_enabled": True,
        "provider": AUTH_PROVIDER,
        "signup_enabled": oidc_auth_enabled(),
        "preferences": {
            "theme": _clean_theme(user.get("preferred_theme")),
        },
        "user": {
            "username": user["username"],
            "display_name": user.get("display_name"),
            "email": user.get("email"),
            "preferred_theme": _clean_theme(user.get("preferred_theme")),
            "is_admin": bool(user["is_admin"]),
            "must_change_password": bool(user["must_change_password"]),
        },
    }


@app.put("/api/auth/preferences")
def update_auth_preferences(
    payload: UserPreferencesPayload, request: Request
) -> dict[str, Any]:
    if not auth_enabled():
        raise HTTPException(status_code=400, detail="Authentication is not enabled.")
    user = _require_user(request)
    theme = _clean_theme(payload.theme)
    now = utc_now_sql()
    with connect() as conn:
        conn.execute(
            """
            UPDATE auth_users
            SET preferred_theme = %s, updated_at = %s
            WHERE id = %s
            """,
            (theme, now, user["id"]),
        )
    request.state.user = {**user, "preferred_theme": theme}
    return {"theme": theme}


def _inventory_owner_id(request: Request) -> int | None:
    if not auth_enabled():
        return None
    return int(_require_user(request)["id"])


def _inventory_owner_clause(
    owner_user_id: int | None, alias: str = "i"
) -> tuple[str, list[Any]]:
    column = f"{alias}.owner_user_id" if alias else "owner_user_id"
    deleted_column = f"{alias}.deleted_at" if alias else "deleted_at"
    visibility_clause = f"{deleted_column} IS NULL"
    if owner_user_id is None:
        return f"{column} IS NULL AND {visibility_clause}", []
    return f"{column} = %s AND {visibility_clause}", [owner_user_id]


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
        options.append(
            {
                "key": key,
                "name": name,
                "kind": _clean_optional(str(option.get("kind") or "")) or "Weapon",
                "stats": {str(k): str(v) for k, v in stats.items() if v is not None},
            }
        )
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
        composition_options = component.get("composition_options")
        if not isinstance(composition_options, list):
            composition_options = []
        components.append(
            {
                "key": key,
                "name": name,
                "min_models": _safe_optional_int(component.get("min_models")),
                "max_models": _safe_optional_int(component.get("max_models")),
                "wargear_options": options,
                "wargear_option_count": len(options),
                "composition_options": [
                    _clean_optional(str(option or ""))
                    for option in composition_options
                    if _clean_optional(str(option or ""))
                ],
                "display_in_composition": component.get("display_in_composition")
                is not False,
            }
        )
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


def _copy_wargear_selection_key(
    component: dict[str, Any], option: dict[str, Any]
) -> str:
    return f"{component['key']}::{option['key']}"


def _wargear_summary_labels(
    options: list[dict[str, Any]],
    model_composition: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, str], dict[str, int]]:
    labels: dict[str, str] = {}
    order: dict[str, int] = {}

    for component in model_composition or []:
        component_name = component.get("name") or ""
        for option in component.get("wargear_options", []):
            key = _copy_wargear_selection_key(component, option)
            labels[key] = (
                f"{component_name}: {option['name']}"
                if component_name
                else option["name"]
            )
            order.setdefault(key, len(order))

    for option in options:
        labels.setdefault(option["key"], option["name"])
        order.setdefault(option["key"], len(order))

    return labels, order


def _format_wargear_summary(
    selections: dict[str, int],
    options: list[dict[str, Any]],
    model_composition: list[dict[str, Any]] | None = None,
) -> str | None:
    if not selections:
        return None
    labels, order = _wargear_summary_labels(options, model_composition)
    parts: list[str] = []
    for key in sorted(
        selections,
        key=lambda item: (order.get(item, len(order)), labels.get(item, item).lower()),
    ):
        amount = selections[key]
        name = labels.get(key) or key
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
    row["model_composition"] = _decode_model_composition(
        row.pop("model_composition_json", None)
    )
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

    notes = _clean_textarea(
        parsed.get("notes") if isinstance(parsed.get("notes"), str) else None
    )
    if notes:
        parts.append(notes)

    return "; ".join(parts)


def _image_url(row: dict[str, Any]) -> str:
    return f"/uploads/inventory/{row['inventory_item_id']}/{row['file_name']}"


def _image_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["url"] = _image_url(item)
    return item


def _image_storage_key(
    inventory_item_id: int, file_name: str, storage_key: str | None = None
) -> str:
    if storage_key:
        return storage_key
    return f"inventory/{inventory_item_id}/{Path(file_name).name}"


def _delete_image_file(
    inventory_item_id: int, file_name: str, storage_key: str | None = None
) -> None:
    delete_object(_image_storage_key(inventory_item_id, file_name, storage_key))


@app.get("/uploads/inventory/{item_id}/{file_name}", include_in_schema=False)
def uploaded_inventory_image(
    request: Request, item_id: int, file_name: str
) -> Response:
    safe_name = Path(file_name).name
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT img.file_name, img.storage_key, img.content_type
            FROM inventory_images img
            JOIN inventory_items i ON i.id = img.inventory_item_id
            WHERE img.inventory_item_id = %s
              AND img.file_name = %s
              AND img.deleted_at IS NULL
              AND {owner_clause}
            """,
            [item_id, safe_name, *owner_params],
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Image not found.")

    try:
        stored = get_object(
            _image_storage_key(item_id, safe_name, row.get("storage_key"))
        )
    except ObjectNotFound:
        raise HTTPException(status_code=404, detail="Image not found.")
    return Response(
        content=stored.content,
        media_type=row.get("content_type")
        or stored.content_type
        or "application/octet-stream",
    )


def _copy_payload_data(
    payload: InventoryCopyPayload,
    wargear_options: list[dict[str, Any]],
    model_composition: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    data = payload.model_dump()
    data["models_owned"] = _safe_optional_int(data.get("models_owned"))
    data["built_count"] = _safe_optional_int(data.get("built_count"))
    data["painted_count"] = _safe_optional_int(data.get("painted_count"))
    data["model_number"] = _clean_optional(data.get("model_number"))
    data["storage_location"] = _clean_optional(data.get("storage_location"))
    data["wargear"] = _clean_textarea(data.get("wargear"))
    data["notes"] = _clean_textarea(data.get("notes"))
    data["wargear_selections"] = _clean_wargear_selections(
        data.get("wargear_selections")
    )
    data["wargear_selections_json"] = (
        json.dumps(data["wargear_selections"], sort_keys=True)
        if data["wargear_selections"]
        else None
    )

    if data["wargear_selections"]:
        data["wargear"] = _format_wargear_summary(
            data["wargear_selections"], wargear_options, model_composition
        )

    return data


def _inventory_copy_dict(row: Any) -> dict[str, Any]:
    copy = dict(row)
    copy["models_owned"] = max(int(copy.get("models_owned") or 0), 0)
    copy["built_count"] = max(int(copy.get("built_count") or 0), 0)
    copy["painted_count"] = max(int(copy.get("painted_count") or 0), 0)
    copy["wargear_selections"] = _decode_wargear_selections(
        copy.pop("wargear_selections_json", None)
    )
    copy.setdefault("images", [])
    return copy


def _copy_progress_seed(total: Any, assigned: int, capacity: Any) -> int:
    remaining = max(int(total or 0) - assigned, 0)
    if remaining <= 0:
        return 0
    copy_capacity = max(int(capacity or 0), 0)
    return min(copy_capacity, remaining) if copy_capacity > 0 else remaining


def _copy_progress_values(total: int, copies: list[Any]) -> list[int]:
    remaining = max(int(total or 0), 0)
    values: list[int] = []
    for copy in copies:
        if remaining <= 0:
            values.append(0)
            continue
        capacity = max(int(copy["models_owned"] or 0), 0)
        value = min(capacity, remaining) if capacity > 0 else remaining
        values.append(value)
        remaining -= value
    return values


def _copy_seed_from_item(item: Any, copy_number: int) -> dict[str, Any]:
    if copy_number != 1:
        return {
            "models_owned": item["models_owned"],
            "built_count": 0,
            "painted_count": 0,
            "model_number": None,
            "wargear": None,
            "wargear_selections_json": None,
            "storage_location": None,
            "notes": None,
        }

    return {
        "models_owned": item["models_owned"],
        "built_count": 0,
        "painted_count": 0,
        "model_number": item["model_number"],
        "wargear": item["wargear"],
        "wargear_selections_json": item["wargear_selections_json"],
        "storage_location": item["storage_location"],
        "notes": item["notes"],
    }


def _sync_inventory_progress_from_copies(
    conn: Any, item_id: int, updated_at: str | None = None
) -> None:
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(c.built_count), 0) AS built_count,
            COALESCE(SUM(c.painted_count), 0) AS painted_count
        FROM inventory_copies c
        JOIN inventory_items i ON i.id = c.inventory_item_id
        WHERE c.inventory_item_id = %s
          AND c.copy_number <= i.quantity
          AND c.deleted_at IS NULL
        """,
        (item_id,),
    ).fetchone()
    built_count = max(int(row["built_count"] or 0), 0) if row else 0
    painted_count = max(int(row["painted_count"] or 0), 0) if row else 0

    if updated_at is None:
        conn.execute(
            """
            UPDATE inventory_items
            SET built_count = %s, painted_count = %s
            WHERE id = %s
            """,
            (built_count, painted_count, item_id),
        )
        return

    conn.execute(
        """
        UPDATE inventory_items
        SET built_count = %s,
            painted_count = %s,
            updated_at = %s,
            version = COALESCE(version, 1) + 1
        WHERE id = %s
        """,
        (built_count, painted_count, updated_at, item_id),
    )


def _ensure_inventory_copies(conn: Any, item: Any) -> None:
    item_id = int(item["id"])
    quantity = max(int(item["quantity"] or 0), 0)
    existing_rows = conn.execute(
        """
        SELECT copy_number, models_owned, built_count, painted_count
        FROM inventory_copies
        WHERE inventory_item_id = %s
          AND deleted_at IS NULL
        """,
        (item_id,),
    ).fetchall()
    existing_numbers = {int(row["copy_number"]) for row in existing_rows}
    built_assigned = sum(max(int(row["built_count"] or 0), 0) for row in existing_rows)
    painted_assigned = sum(
        max(int(row["painted_count"] or 0), 0) for row in existing_rows
    )
    now = utc_now_sql()

    for copy_number in range(1, quantity + 1):
        if copy_number in existing_numbers:
            continue
        seed = _copy_seed_from_item(
            item, copy_number if existing_numbers else copy_number
        )
        seed["built_count"] = _copy_progress_seed(
            item["built_count"], built_assigned, seed["models_owned"]
        )
        seed["painted_count"] = _copy_progress_seed(
            item["painted_count"], painted_assigned, seed["models_owned"]
        )
        built_assigned += seed["built_count"]
        painted_assigned += seed["painted_count"]
        conn.execute(
            """
            INSERT INTO inventory_copies (
                public_id, inventory_item_id, copy_number, models_owned, built_count, painted_count, model_number,
                wargear, wargear_selections_json, storage_location, notes,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                _new_public_id(),
                item_id,
                copy_number,
                seed["models_owned"],
                seed["built_count"],
                seed["painted_count"],
                seed["model_number"],
                seed["wargear"],
                seed["wargear_selections_json"],
                seed["storage_location"],
                seed["notes"],
                now,
                now,
            ),
        )

    _sync_inventory_progress_from_copies(conn, item_id)

    if quantity > 0:
        first_copy = conn.execute(
            """
            SELECT id FROM inventory_copies
            WHERE inventory_item_id = %s AND copy_number = 1
              AND deleted_at IS NULL
            """,
            (item_id,),
        ).fetchone()
        if first_copy is not None:
            conn.execute(
                """
                UPDATE inventory_images
                SET inventory_copy_id = %s
                WHERE inventory_item_id = %s AND inventory_copy_id IS NULL
                """,
                (first_copy["id"], item_id),
            )


def _apply_imported_item_to_copies(
    conn: Any,
    item_id: int,
    *,
    built_count: int,
    painted_count: int,
    model_number: str | None,
    wargear: str | None,
    wargear_selections_json: str | None,
    storage_location: str | None,
    notes: str | None,
) -> None:
    copies = conn.execute(
        """
        SELECT c.id, c.copy_number, c.models_owned
        FROM inventory_copies c
        JOIN inventory_items i ON i.id = c.inventory_item_id
        WHERE c.inventory_item_id = %s
          AND c.copy_number <= i.quantity
          AND c.deleted_at IS NULL
        ORDER BY c.copy_number
        """,
        (item_id,),
    ).fetchall()
    if not copies:
        return

    built_values = _copy_progress_values(built_count, copies)
    painted_values = _copy_progress_values(painted_count, copies)
    now = utc_now_sql()
    for index, copy in enumerate(copies):
        if int(copy["copy_number"]) == 1:
            conn.execute(
                """
                UPDATE inventory_copies
                SET built_count = %s,
                    painted_count = %s,
                    model_number = %s,
                    wargear = %s,
                    wargear_selections_json = %s,
                    storage_location = %s,
                    notes = %s,
                    updated_at = %s,
                    version = COALESCE(version, 1) + 1
                WHERE id = %s
                """,
                (
                    built_values[index],
                    painted_values[index],
                    model_number,
                    wargear,
                    wargear_selections_json,
                    storage_location,
                    notes,
                    now,
                    copy["id"],
                ),
            )
        else:
            conn.execute(
                """
                UPDATE inventory_copies
                SET built_count = %s,
                    painted_count = %s,
                    updated_at = %s,
                    version = COALESCE(version, 1) + 1
                WHERE id = %s
                """,
                (built_values[index], painted_values[index], now, copy["id"]),
            )
    _sync_inventory_progress_from_copies(conn, item_id, now)


def _trim_inventory_copies(conn: Any, item_id: int, quantity: int) -> list[Any]:
    # Keep out-of-range copy details so accidental quantity reductions do not
    # destroy per-copy notes/photos. The list response only exposes copies up
    # to the current quantity, and raising quantity later reveals them again.
    return []


def _inventory_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    item.pop("owner_user_id", None)
    item["unbuilt_count"] = max(
        int(item.get("models_owned") or 0) - int(item.get("built_count") or 0), 0
    )
    item["unpainted_count"] = max(
        int(item.get("models_owned") or 0) - int(item.get("painted_count") or 0), 0
    )
    item["wargear_selections"] = _decode_wargear_selections(
        item.pop("wargear_selections_json", None)
    )
    item["wargear_options"] = _decode_wargear_options(
        item.pop("current_wargear_options_json", None)
    )
    item["model_composition"] = _decode_model_composition(
        item.pop("current_model_composition_json", None)
    )
    item.setdefault("images", [])
    return item


def _attach_copies(conn: Any, items: list[dict[str, Any]]) -> None:
    for item in items:
        item["copies"] = []
    if not items:
        return

    ids = [int(item["id"]) for item in items]
    placeholders = ",".join("%s" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT c.*
        FROM inventory_copies c
        JOIN inventory_items i ON i.id = c.inventory_item_id
        WHERE c.inventory_item_id IN ({placeholders})
          AND c.copy_number <= i.quantity
          AND c.deleted_at IS NULL
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


def _apply_copy_progress_totals(items: list[dict[str, Any]]) -> None:
    for item in items:
        copies = item.get("copies") or []
        if not copies:
            continue
        built_count = sum(max(int(copy.get("built_count") or 0), 0) for copy in copies)
        painted_count = sum(
            max(int(copy.get("painted_count") or 0), 0) for copy in copies
        )
        item["built_count"] = built_count
        item["painted_count"] = painted_count
        item["unbuilt_count"] = max(int(item.get("models_owned") or 0) - built_count, 0)
        item["unpainted_count"] = max(
            int(item.get("models_owned") or 0) - painted_count, 0
        )


def _attach_images(conn: Any, items: list[dict[str, Any]]) -> None:
    for item in items:
        item["images"] = []
        for copy in item.get("copies", []):
            copy["images"] = []
    if not items:
        return

    ids = [int(item["id"]) for item in items]
    placeholders = ",".join("%s" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT * FROM inventory_images
        WHERE inventory_item_id IN ({placeholders})
          AND deleted_at IS NULL
        ORDER BY created_at DESC, id DESC
        """,
        ids,
    ).fetchall()
    by_item = {int(item["id"]): item for item in items}
    by_copy = {
        int(copy["id"]): copy for item in items for copy in item.get("copies", [])
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
    data["wargear_selections"] = _clean_wargear_selections(
        data.get("wargear_selections")
    )
    data["wargear_selections_json"] = (
        json.dumps(data["wargear_selections"], sort_keys=True)
        if data["wargear_selections"]
        else None
    )
    data["notes"] = _clean_textarea(data.get("notes"))

    wargear_options: list[dict[str, Any]] = []
    model_composition: list[dict[str, Any]] = []
    if data.get("unit_id") is not None:
        unit = conn.execute(
            """
            SELECT id, game_system, name, faction, catalogue_file,
                   wargear_options_json, model_composition_json
            FROM bsd_units
            WHERE id = %s AND deleted_at IS NULL
            """,
            (data["unit_id"],),
        ).fetchone()
        if unit is None:
            raise HTTPException(
                status_code=404,
                detail="Catalogue entry not found. Wait for BSData sync to finish, then search again.",
            )
        data["game_system"] = unit["game_system"]
        data["unit_name"] = unit["name"]
        data["faction"] = data["faction"] or unit["faction"]
        data["catalogue_file"] = data["catalogue_file"] or unit["catalogue_file"]
        wargear_options = _decode_wargear_options(unit["wargear_options_json"])
        model_composition = _decode_model_composition(unit["model_composition_json"])

    if data["wargear_selections"]:
        data["wargear"] = _format_wargear_summary(
            data["wargear_selections"], wargear_options, model_composition
        )

    if not data.get("unit_name"):
        raise HTTPException(
            status_code=400, detail="unit_name is required for custom inventory items."
        )

    return data


def _inventory_item_response(
    conn: Any, item_id: int, owner_user_id: int | None
) -> dict[str, Any]:
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
        WHERE i.id = %s AND {owner_clause}
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
    _apply_copy_progress_totals([item])
    _attach_images(conn, [item])
    return item


def _item_wargear_catalogue(
    conn: Any,
    item_id: int,
    owner_user_id: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    row = conn.execute(
        f"""
        SELECT u.wargear_options_json, u.model_composition_json
        FROM inventory_items i
        LEFT JOIN bsd_units u ON u.id = i.unit_id
        WHERE i.id = %s AND {owner_clause}
        """,
        [item_id, *owner_params],
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Inventory item not found.")
    return (
        _decode_wargear_options(row["wargear_options_json"]),
        _decode_model_composition(row["model_composition_json"]),
    )


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

    raise HTTPException(
        status_code=400, detail="Upload must be a JPG, PNG, WebP, or GIF image."
    )


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
def status(
    request: Request, game_system: str = Query(default=DEFAULT_GAME_SYSTEM)
) -> dict[str, Any]:
    config = _config_or_400(game_system)
    bsdata_dir = _bsdata_dir(config)
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    with connect() as conn:
        last_run = conn.execute(
            "SELECT * FROM import_runs WHERE game_system = %s ORDER BY id DESC LIMIT 1",
            (config.id,),
        ).fetchone()
        active_count = conn.execute(
            "SELECT COUNT(*) AS count FROM bsd_units WHERE game_system = %s AND active = 1 AND deleted_at IS NULL",
            (config.id,),
        ).fetchone()["count"]
        unit_count = conn.execute(
            "SELECT COUNT(*) AS count FROM bsd_units WHERE game_system = %s",
            (config.id,),
        ).fetchone()["count"]
        inventory_count = conn.execute(
            f"SELECT COUNT(*) AS count FROM inventory_items i WHERE i.game_system = %s AND {owner_clause}",
            [config.id, *owner_params],
        ).fetchone()["count"]
        image_count = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM inventory_images img
            JOIN inventory_items i ON i.id = img.inventory_item_id
            WHERE i.game_system = %s AND img.deleted_at IS NULL AND {owner_clause}
            """,
            [config.id, *owner_params],
        ).fetchone()["count"]
        return {
            "game_system": config.id,
            "game_label": config.label,
            "database_url": database_label(),
            "storage_backend": storage_label(),
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


def _run_with_bsdata_sync_lock(callback):
    if not _BSDATA_SYNC_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A BSData sync is already running.")
    try:
        return callback()
    finally:
        _BSDATA_SYNC_LOCK.release()


def _sync_game_system_unlocked(config: GameSystemConfig) -> dict[str, Any]:
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
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ) VALUES (%s, %s, %s, 'failed', %s, %s, 0, 0, %s)
                """,
                (
                    config.id,
                    started_at,
                    finished_at,
                    str(exc),
                    repo_message,
                    json.dumps([str(exc)]),
                ),
            )
        raise HTTPException(
            status_code=500, detail=f"{config.label} BSData sync failed: {exc}"
        ) from exc


def _sync_game_system(config: GameSystemConfig) -> dict[str, Any]:
    return _run_with_bsdata_sync_lock(lambda: _sync_game_system_unlocked(config))


def _sync_all_game_systems() -> dict[str, Any]:
    def run() -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for config in GAME_SYSTEMS.values():
            try:
                results.append(_sync_game_system_unlocked(config))
            except HTTPException as exc:
                failures.append(
                    {
                        "game_system": config.id,
                        "game_label": config.label,
                        "status_code": exc.status_code,
                        "detail": exc.detail,
                    }
                )
        status_text = "success"
        if failures:
            status_text = "failed" if not results else "success_with_errors"
        return {
            "status": status_text,
            "results": results,
            "failures": failures,
        }

    return _run_with_bsdata_sync_lock(run)


@app.post("/api/sync")
def sync_bsdata(
    request: Request, game_system: str = Query(default=DEFAULT_GAME_SYSTEM)
) -> dict[str, Any]:
    if auth_enabled():
        _require_admin(request)
    return _sync_game_system(_config_or_400(game_system))


@app.post("/api/sync/{game_system}")
def sync_bsdata_for_system(request: Request, game_system: str) -> dict[str, Any]:
    if auth_enabled():
        _require_admin(request)
    return _sync_game_system(_config_or_400(game_system))


@app.get("/api/factions")
def factions(
    game_system: str = Query(default=DEFAULT_GAME_SYSTEM),
) -> list[dict[str, Any]]:
    config = _config_or_400(game_system)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT faction, COUNT(*) AS unit_count
            FROM bsd_units
            WHERE game_system = %s AND active = 1
            GROUP BY faction
            ORDER BY lower(faction)
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
        SELECT id, public_id, game_system, bs_id, name, faction, catalogue_file, entry_type, points,
               min_models, max_models, keywords, stats_json, wargear_options_json,
               model_composition_json, imported_at, version, deleted_at
        FROM bsd_units
        WHERE game_system = %s AND active = 1 AND deleted_at IS NULL
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
                for column in (
                    "name",
                    "faction",
                    "keywords",
                    "entry_type",
                    "catalogue_file",
                ):
                    conditions.append(f"{column} ILIKE %s")
                    params.append(like)
            sql.append("AND (" + " OR ".join(conditions) + ")")

    cleaned_faction = _clean_optional(faction)
    if cleaned_faction:
        sql.append("AND faction = %s")
        params.append(cleaned_faction)

    sql.append("ORDER BY lower(faction), lower(name) LIMIT %s")
    params.append(limit)

    with connect() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()
        return [_decode_stats(dict(row)) for row in rows]


@app.get("/api/inventory")
def inventory(
    request: Request, game_system: str = Query(default=DEFAULT_GAME_SYSTEM)
) -> list[dict[str, Any]]:
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
            WHERE i.game_system = %s AND {owner_clause}
            ORDER BY lower(COALESCE(i.faction, '')), lower(i.unit_name), i.id
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
        _apply_copy_progress_totals(output)
        _attach_images(conn, output)
        return output


@app.post("/api/inventory", status_code=201)
def create_inventory_item(
    request: Request, payload: InventoryPayload
) -> dict[str, Any]:
    now = utc_now_sql()
    owner_user_id = _inventory_owner_id(request)
    with connect() as conn:
        data = _payload_with_unit_snapshot(payload, conn)
        cursor = conn.execute(
            """
            INSERT INTO inventory_items (
                public_id, owner_user_id, game_system, unit_id, unit_name, faction, catalogue_file, quantity, models_owned,
                built_count, painted_count, wargear, wargear_selections_json, model_number, storage_location, notes, acquired_on,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                _new_public_id(),
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
        item_id = int(cursor.fetchone()["id"])
        row = conn.execute(
            "SELECT * FROM inventory_items WHERE id = %s", (item_id,)
        ).fetchone()
        _ensure_inventory_copies(conn, row)
        return _inventory_item_response(conn, item_id, owner_user_id)


@app.put("/api/inventory/{item_id}")
def update_inventory_item(
    request: Request, item_id: int, payload: InventoryPayload
) -> dict[str, Any]:
    now = utc_now_sql()
    image_rows_to_delete: list[Any] = []
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    owner_update_clause, owner_update_params = _inventory_owner_clause(
        owner_user_id, alias=""
    )
    with connect() as conn:
        existing = conn.execute(
            f"SELECT id FROM inventory_items i WHERE i.id = %s AND {owner_clause}",
            [item_id, *owner_params],
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Inventory item not found.")

        data = _payload_with_unit_snapshot(payload, conn)
        conn.execute(
            f"""
            UPDATE inventory_items SET
                game_system = %s,
                unit_id = %s,
                unit_name = %s,
                faction = %s,
                catalogue_file = %s,
                quantity = %s,
                models_owned = %s,
                built_count = %s,
                painted_count = %s,
                wargear = %s,
                wargear_selections_json = %s,
                model_number = %s,
                storage_location = %s,
                notes = %s,
                acquired_on = %s,
                updated_at = %s,
                version = COALESCE(version, 1) + 1
            WHERE id = %s AND {owner_update_clause}
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
            f"SELECT * FROM inventory_items i WHERE i.id = %s AND {owner_clause}",
            [item_id, *owner_params],
        ).fetchone()
        _ensure_inventory_copies(conn, row)
        item = _inventory_item_response(conn, item_id, owner_user_id)

    for row in image_rows_to_delete:
        _delete_image_file(
            int(row["inventory_item_id"]), row["file_name"], row.get("storage_key")
        )
    return item


@app.put("/api/inventory/{item_id}/copies/{copy_id}")
def update_inventory_copy(
    request: Request, item_id: int, copy_id: int, payload: InventoryCopyPayload
) -> dict[str, Any]:
    now = utc_now_sql()
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    with connect() as conn:
        copy_row = conn.execute(
            f"""
            SELECT c.*
            FROM inventory_copies c
            JOIN inventory_items i ON i.id = c.inventory_item_id
            WHERE c.id = %s
              AND c.inventory_item_id = %s
              AND c.deleted_at IS NULL
              AND {owner_clause}
            """,
            [copy_id, item_id, *owner_params],
        ).fetchone()
        if copy_row is None:
            raise HTTPException(status_code=404, detail="Inventory copy not found.")

        wargear_options, model_composition = _item_wargear_catalogue(
            conn, item_id, owner_user_id
        )
        data = _copy_payload_data(payload, wargear_options, model_composition)
        conn.execute(
            f"""
            UPDATE inventory_copies SET
                models_owned = COALESCE(%s, models_owned),
                built_count = COALESCE(%s, built_count),
                painted_count = COALESCE(%s, painted_count),
                model_number = %s,
                wargear = %s,
                wargear_selections_json = %s,
                storage_location = %s,
                notes = %s,
                updated_at = %s,
                version = COALESCE(version, 1) + 1
            WHERE id = %s AND inventory_item_id = %s
              AND deleted_at IS NULL
              AND EXISTS (
                SELECT 1
                FROM inventory_items i
                WHERE i.id = inventory_copies.inventory_item_id AND {owner_clause}
              )
            """,
            [
                data["models_owned"],
                data["built_count"],
                data["painted_count"],
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
        _sync_inventory_progress_from_copies(conn, item_id, now)
        row = conn.execute(
            "SELECT * FROM inventory_copies WHERE id = %s", (copy_id,)
        ).fetchone()
        copy = _inventory_copy_dict(row)
        parent = {"id": item_id, "copies": [copy], "images": []}
        _attach_images(conn, [parent])
        return copy


@app.delete("/api/inventory/{item_id}", status_code=204)
def delete_inventory_item(request: Request, item_id: int) -> Response:
    now = utc_now_sql()
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    owner_delete_clause, owner_delete_params = _inventory_owner_clause(
        owner_user_id, alias=""
    )
    with connect() as conn:
        image_rows = conn.execute(
            f"""
            SELECT img.inventory_item_id, img.file_name, img.storage_key
            FROM inventory_images img
            JOIN inventory_items i ON i.id = img.inventory_item_id
            WHERE img.inventory_item_id = %s
              AND img.deleted_at IS NULL
              AND {owner_clause}
            """,
            [item_id, *owner_params],
        ).fetchall()
        cursor = conn.execute(
            f"""
            UPDATE inventory_items
            SET deleted_at = %s,
                updated_at = %s,
                version = COALESCE(version, 1) + 1
            WHERE id = %s AND {owner_delete_clause}
            """,
            [now, now, item_id, *owner_delete_params],
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Inventory item not found.")
        conn.execute(
            """
            UPDATE inventory_copies
            SET deleted_at = COALESCE(deleted_at, %s),
                updated_at = %s,
                version = COALESCE(version, 1) + 1
            WHERE inventory_item_id = %s
              AND deleted_at IS NULL
            """,
            (now, now, item_id),
        )
        conn.execute(
            """
            UPDATE inventory_images
            SET deleted_at = COALESCE(deleted_at, %s),
                version = COALESCE(version, 1) + 1
            WHERE inventory_item_id = %s
              AND deleted_at IS NULL
            """,
            (now, item_id),
        )

    for row in image_rows:
        _delete_image_file(
            int(row["inventory_item_id"]), row["file_name"], row.get("storage_key")
        )
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
            f"SELECT id, public_id FROM inventory_items i WHERE i.id = %s AND {owner_clause}",
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
                WHERE c.id = %s
                  AND c.inventory_item_id = %s
                  AND c.deleted_at IS NULL
                  AND {owner_clause}
                """,
                [copy_id, item_id, *owner_params],
            ).fetchone()
            if copy is None:
                raise HTTPException(status_code=404, detail="Inventory copy not found.")

    ext = _safe_image_ext(image)
    content = await image.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413, detail="Image is too large. Maximum size is 12 MB."
        )
    if not content:
        raise HTTPException(status_code=400, detail="Image upload was empty.")

    file_name = f"{uuid.uuid4().hex}{ext}"
    content_type = (image.content_type or "").split(";", 1)[0].lower() or None
    storage_key = f"inventory/{item.get('public_id') or item_id}/{file_name}"
    put_object(storage_key, content, content_type)

    now = utc_now_sql()
    try:
        with connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO inventory_images (
                    public_id, inventory_item_id, inventory_copy_id, file_name, storage_key, original_name, content_type,
                    image_role, caption, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    _new_public_id(),
                    item_id,
                    copy_id,
                    file_name,
                    storage_key,
                    _original_name(image.filename),
                    content_type,
                    _safe_image_role(image_role),
                    _clean_textarea(caption),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM inventory_images WHERE id = %s",
                (cursor.fetchone()["id"],),
            ).fetchone()
            return _image_dict(row)
    except Exception:
        _delete_image_file(item_id, file_name, storage_key)
        raise


@app.post("/api/inventory/{item_id}/images", status_code=201)
async def upload_inventory_image(
    request: Request,
    item_id: int,
    image: UploadFile = File(...),
    image_role: str = Form(default="other"),
    caption: str | None = Form(default=None),
) -> dict[str, Any]:
    return await _store_inventory_image(
        _inventory_owner_id(request), item_id, image, image_role, caption
    )


@app.post("/api/inventory/{item_id}/copies/{copy_id}/images", status_code=201)
async def upload_inventory_copy_image(
    request: Request,
    item_id: int,
    copy_id: int,
    image: UploadFile = File(...),
    image_role: str = Form(default="other"),
    caption: str | None = Form(default=None),
) -> dict[str, Any]:
    return await _store_inventory_image(
        _inventory_owner_id(request), item_id, image, image_role, caption, copy_id
    )


@app.delete("/api/images/{image_id}", status_code=204)
def delete_inventory_image(request: Request, image_id: int) -> Response:
    now = utc_now_sql()
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT img.*
            FROM inventory_images img
            JOIN inventory_items i ON i.id = img.inventory_item_id
            WHERE img.id = %s
              AND img.deleted_at IS NULL
              AND {owner_clause}
            """,
            [image_id, *owner_params],
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Image not found.")
        conn.execute(
            """
            UPDATE inventory_images
            SET deleted_at = %s,
                version = COALESCE(version, 1) + 1
            WHERE id = %s
            """,
            (now, image_id),
        )

    _delete_image_file(
        int(row["inventory_item_id"]), row["file_name"], row.get("storage_key")
    )
    return Response(status_code=204)


def _csv_row_has_data(row: dict[str, Any]) -> bool:
    return any(str(value or "").strip() for value in row.values())


def _csv_text(row: dict[str, Any], field: str, *, textarea: bool = False) -> str | None:
    value = row.get(field)
    if value is None:
        return None
    return _clean_textarea(value) if textarea else _clean_optional(value)


def _csv_int(row: dict[str, Any], field: str, *, default: int = 0) -> int:
    value = row.get(field)
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{field} must be a non-negative integer.") from exc
    if parsed < 0:
        raise ValueError(f"{field} must be a non-negative integer.")
    return parsed


def _csv_public_id(row: dict[str, Any]) -> str | None:
    value = _csv_text(row, "public_id")
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _csv_wargear_selections(row: dict[str, Any]) -> dict[str, int]:
    value = _csv_text(row, "wargear_selections", textarea=True)
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("wargear_selections must be a JSON object.") from exc
    if not isinstance(decoded, dict):
        raise ValueError("wargear_selections must be a JSON object.")
    return _clean_wargear_selections(decoded)


def _owner_matches(row_owner_user_id: Any, owner_user_id: int | None) -> bool:
    if row_owner_user_id is None:
        return owner_user_id is None
    if owner_user_id is None:
        return False
    return int(row_owner_user_id) == int(owner_user_id)


def _find_import_unit_id(
    conn: Any,
    *,
    game_system: str,
    unit_name: str,
    faction: str | None,
    catalogue_file: str | None,
) -> int | None:
    sql = [
        """
        SELECT id
        FROM bsd_units
        WHERE game_system = %s
          AND active = 1
          AND deleted_at IS NULL
          AND lower(name) = lower(%s)
        """
    ]
    params: list[Any] = [game_system, unit_name]
    if catalogue_file:
        sql.append("AND lower(catalogue_file) = lower(%s)")
        params.append(catalogue_file)
    if faction:
        sql.append("AND lower(faction) = lower(%s)")
        params.append(faction)
    sql.append("ORDER BY id DESC LIMIT 1")
    row = conn.execute(" ".join(sql), params).fetchone()
    return int(row["id"]) if row is not None else None


def _csv_inventory_data(
    row: dict[str, Any], config: GameSystemConfig, conn: Any
) -> dict[str, Any]:
    row_game_system = _csv_text(row, "game_system") or config.id
    try:
        row_config = get_game_system_config(row_game_system)
    except UnknownGameSystem as exc:
        raise ValueError(f"Unknown game_system '{row_game_system}'.") from exc
    if row_config.id != config.id:
        raise ValueError(
            f"CSV row is for {row_config.label}; import it from that game tab."
        )

    unit_name = _csv_text(row, "unit_name")
    if not unit_name:
        raise ValueError("unit_name is required.")

    faction = _csv_text(row, "faction")
    catalogue_file = _csv_text(row, "catalogue_file")
    wargear_selections = _csv_wargear_selections(row)
    unit_id = _find_import_unit_id(
        conn,
        game_system=config.id,
        unit_name=unit_name,
        faction=faction,
        catalogue_file=catalogue_file,
    )
    wargear_selections_json = (
        json.dumps(wargear_selections, sort_keys=True) if wargear_selections else None
    )

    return {
        "public_id": _csv_public_id(row) or _new_public_id(),
        "game_system": config.id,
        "unit_id": unit_id,
        "unit_name": unit_name,
        "faction": faction,
        "catalogue_file": catalogue_file,
        "quantity": _csv_int(row, "quantity", default=1),
        "models_owned": _csv_int(row, "models_owned"),
        "built_count": _csv_int(row, "built_count"),
        "painted_count": _csv_int(row, "painted_count"),
        "wargear": _csv_text(row, "wargear", textarea=True),
        "wargear_selections_json": wargear_selections_json,
        "model_number": _csv_text(row, "model_number"),
        "storage_location": _csv_text(row, "storage_location"),
        "acquired_on": _csv_text(row, "acquired_on"),
        "notes": _csv_text(row, "notes", textarea=True),
    }


def _import_inventory_csv_row(
    conn: Any,
    row: dict[str, Any],
    config: GameSystemConfig,
    owner_user_id: int | None,
) -> str:
    data = _csv_inventory_data(row, config, conn)
    now = utc_now_sql()
    existing = conn.execute(
        "SELECT id, owner_user_id FROM inventory_items WHERE public_id = %s",
        (data["public_id"],),
    ).fetchone()
    if existing is not None and not _owner_matches(
        existing["owner_user_id"], owner_user_id
    ):
        data["public_id"] = _new_public_id()
        existing = None

    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO inventory_items (
                public_id, owner_user_id, game_system, unit_id, unit_name, faction, catalogue_file,
                quantity, models_owned, built_count, painted_count, wargear, wargear_selections_json,
                model_number, storage_location, notes, acquired_on, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                data["public_id"],
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
        item_id = int(cursor.fetchone()["id"])
        action = "created"
    else:
        item_id = int(existing["id"])
        conn.execute(
            """
            UPDATE inventory_items
            SET game_system = %s,
                unit_id = %s,
                unit_name = %s,
                faction = %s,
                catalogue_file = %s,
                quantity = %s,
                models_owned = %s,
                built_count = %s,
                painted_count = %s,
                wargear = %s,
                wargear_selections_json = %s,
                model_number = %s,
                storage_location = %s,
                notes = %s,
                acquired_on = %s,
                updated_at = %s,
                deleted_at = NULL,
                version = COALESCE(version, 1) + 1
            WHERE id = %s
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
        conn.execute(
            """
            UPDATE inventory_copies
            SET deleted_at = NULL,
                updated_at = %s,
                version = COALESCE(version, 1) + 1
            WHERE inventory_item_id = %s
              AND copy_number <= %s
            """,
            (now, item_id, data["quantity"]),
        )
        action = "updated"

    item = conn.execute(
        "SELECT * FROM inventory_items WHERE id = %s", (item_id,)
    ).fetchone()
    _ensure_inventory_copies(conn, item)
    _apply_imported_item_to_copies(
        conn,
        item_id,
        built_count=data["built_count"],
        painted_count=data["painted_count"],
        model_number=data["model_number"],
        wargear=data["wargear"],
        wargear_selections_json=data["wargear_selections_json"],
        storage_location=data["storage_location"],
        notes=data["notes"],
    )
    return action


@app.post("/api/import.csv")
async def import_inventory_csv(
    request: Request,
    game_system: str = Query(default=DEFAULT_GAME_SYSTEM),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    config = _config_or_400(game_system)
    owner_user_id = _inventory_owner_id(request)
    content = await file.read(MAX_CSV_IMPORT_BYTES + 1)
    if len(content) > MAX_CSV_IMPORT_BYTES:
        raise HTTPException(
            status_code=413, detail="CSV is too large. Maximum size is 2 MB."
        )
    if not content:
        raise HTTPException(status_code=400, detail="CSV upload was empty.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400, detail="CSV must be UTF-8 encoded."
        ) from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV has no header row.")
    missing = {"unit_name"} - {field.strip() for field in reader.fieldnames if field}
    if missing:
        raise HTTPException(
            status_code=400,
            detail="CSV does not look like a Warhammer Stock Tracker export.",
        )

    created = 0
    updated = 0
    skipped = 0
    with connect() as conn:
        for row_number, row in enumerate(reader, start=2):
            if not _csv_row_has_data(row):
                skipped += 1
                continue
            try:
                action = _import_inventory_csv_row(conn, row, config, owner_user_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"Row {row_number}: {exc}"
                ) from exc
            if action == "created":
                created += 1
            else:
                updated += 1

    return {
        "game_system": config.id,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "imported": created + updated,
    }


@app.get("/api/export.csv")
def export_inventory_csv(
    request: Request, game_system: str = Query(default=DEFAULT_GAME_SYSTEM)
) -> Response:
    config = _config_or_400(game_system)
    owner_user_id = _inventory_owner_id(request)
    owner_clause, owner_params = _inventory_owner_clause(owner_user_id)
    with connect() as conn:
        rows = conn.execute(
            f"""
            WITH copy_progress AS (
                SELECT
                    c.inventory_item_id,
                    COALESCE(SUM(c.built_count), 0) AS built_count,
                    COALESCE(SUM(c.painted_count), 0) AS painted_count
                FROM inventory_copies c
                JOIN inventory_items parent ON parent.id = c.inventory_item_id
                WHERE c.copy_number <= parent.quantity
                GROUP BY c.inventory_item_id
            )
            SELECT
                i.id,
                i.public_id,
                i.game_system,
                i.unit_name,
                i.faction,
                i.catalogue_file,
                i.quantity,
                i.models_owned,
                COALESCE(cp.built_count, i.built_count) AS built_count,
                COALESCE(cp.painted_count, i.painted_count) AS painted_count,
                GREATEST(i.models_owned - COALESCE(cp.built_count, i.built_count), 0) AS unbuilt_count,
                GREATEST(i.models_owned - COALESCE(cp.painted_count, i.painted_count), 0) AS unpainted_count,
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
                (SELECT COUNT(*) FROM inventory_images img WHERE img.inventory_item_id = i.id AND img.deleted_at IS NULL) AS image_count,
                i.created_at,
                i.updated_at,
                i.version
            FROM inventory_items i
            LEFT JOIN bsd_units u ON u.id = i.unit_id
            LEFT JOIN copy_progress cp ON cp.inventory_item_id = i.id
            WHERE i.game_system = %s AND {owner_clause}
            ORDER BY lower(i.unit_name)
            """,
            [config.id, *owner_params],
        ).fetchall()

    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=INVENTORY_CSV_FIELDNAMES)
    writer.writeheader()
    for row in rows:
        output_row = {key: dict(row).get(key) for key in INVENTORY_CSV_FIELDNAMES}
        output_row["wargear"] = _wargear_text(dict(row).get("wargear"))
        writer.writerow(output_row)

    filename = f"warhammer_inventory_{config.id}.csv"
    return Response(
        content=stream.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
