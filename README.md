# Warhammer Stock Tracker

A small Python web app for tracking which Warhammer 40,000, Kill Team, and Age of Sigmar models you own. It uses:

- **FastAPI** for the web app and JSON API
- **Postgres** for shared application data
- **S3-compatible object storage** for uploaded photos, with MinIO used locally
- **Vanilla HTML/CSS/JavaScript** for the front end
- **BSData/wh40k-10e** for Warhammer 40,000 catalogue data
- **BSData/wh40k-killteam** for Kill Team catalogue data
- **BSData/age-of-sigmar-4th** for Age of Sigmar catalogue data

In Docker Compose, Postgres stores inventory/auth/catalogue data, MinIO stores uploaded photos, and `/app/data` is only used for downloaded BSData working files.

## Features

- Top tabs for **40k**, **Kill Team**, and **AoS**.
- Sync each game system separately from BSData.
- Parse `.cat` files and import names, factions/teams, points when present, valid unit sizes where exposed, keywords, basic stats, unit/model datasheet entries, and weapon profile options where BSData exposes them.
- Search imported catalogue entries and add them to your collection.
- Add custom rows for boxed sets, kitbashes, terrain, spare models, bespoke Kill Team operatives, AoS projects, or anything not in BSData.
- Track quantity, models owned, built count, painted count, build backlog, paint backlog, storage location, and notes.
- Split each inventory row into per-quantity copy boxes for base number, wargear, photos, location, and copy notes.
- Collapse inventory rows to hide copy boxes while keeping totals and backlog visible.
- Track **wargear built on the model** from an imported weapon list, with per-weapon quantity controls.
- Track **model number(s)**, for example a number written under a figure base.
- Upload photos for each inventory row and mark each photo as built, painted, WIP, reference, or other.
- Export the current tab's inventory as CSV.
- Keycloak-backed login and self-service signup in the local Compose stack, with JWT support for API/mobile clients.
- Runs locally with Postgres and MinIO, matching the hosted database/object-storage shape.

## Screenshots

![Tracker overview](docs/screenshots/tracker-overview.png)

![Inventory section with summary](docs/screenshots/inventory-section.png)

![Age of Sigmar catalogue tab](docs/screenshots/aos-catalogue.png)

## Quick start

Use Docker Compose for the complete local stack:

```bash
docker compose up --build
```

Open the app at:

```text
http://127.0.0.1:8000
```

Compose starts with authentication enabled. Use `http://127.0.0.1:8000/signup` to create a user through Keycloak, or sign in from the app login page.

Keycloak is available at `http://localhost:8081`. The development admin login is `admin` / `admin-password`.

MinIO is available at `http://127.0.0.1:9001` with username `wh40k` and password `wh40k-secret`.

BSData sync runs automatically when the app starts and then every night at local midnight. Admins can also trigger it manually from `/admin`. Set `WH40K_BSDATA_AUTO_SYNC_ENABLED=false` to disable the automatic scheduler.

## Python development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Start Postgres and MinIO first, then run the app against those local services:

```bash
docker compose up -d postgres minio minio-init
DATABASE_URL=postgresql://wh40k:wh40k@127.0.0.1:5432/wh40k \
STORAGE_BACKEND=s3 \
S3_ENDPOINT_URL=http://127.0.0.1:9000 \
S3_BUCKET=wh40k-uploads \
AWS_ACCESS_KEY_ID=wh40k \
AWS_SECRET_ACCESS_KEY=wh40k-secret \
python run.py
```

To run the local dev server with login enabled:

```bash
python run.py --auth
```

Set `WH40K_PORT` to change the listen port without passing `--port`:

```bash
WH40K_PORT=9000 python run.py
```

## Optional Docker run

Run the full local stack with Compose:

```bash
docker compose up --build
```

Postgres data, MinIO objects, and BSData working files are stored in named Docker volumes and survive container recreation.

To run Docker on another port, set `WH40K_PORT` for both the app and Compose port mapping:

```bash
WH40K_PORT=9000 docker compose up --build
```

## Local cloud-style development stack

For the web/mobile sync migration, use the dev Compose stack. It pulls the published dev app image and starts Postgres plus MinIO as local stand-ins for hosted database and S3 storage:

```bash
docker compose -f docker-compose.dev.yml pull web
docker compose -f docker-compose.dev.yml up
```

This stack uses Postgres and MinIO through the same backend adapters as the app. See [docs/development/local-container-stack.md](docs/development/local-container-stack.md).

## Authentication

Docker Compose defaults to provider-backed auth:

```bash
docker compose up --build
```

The default provider is Keycloak. It runs locally, stores its state in the Postgres `keycloak` schema, and imports the `wh40k` realm only when it is missing. Public registration is enabled, so `/signup` sends users to the Keycloak registration page and returns them to the tracker after login. The local registration profile asks for username, email, and password only.

The local Keycloak realm uses the `night-lords` login theme from `infra/keycloak/themes/night-lords`, which gives the login and registration screens a midnight-blue, steel, red, brass, and lightning-accented treatment.

Useful local URLs:

- App: `http://127.0.0.1:8000`
- Signup: `http://127.0.0.1:8000/signup`
- Keycloak: `http://localhost:8081`
- Keycloak admin console: `http://localhost:8081/admin/master/console/#/wh40k`
- MinIO console: `http://127.0.0.1:9001`

Development credentials:

- Keycloak admin: `admin` / `admin-password`
- MinIO: `wh40k` / `wh40k-secret`

Useful auth environment variables:

- `WH40K_AUTH_ENABLED=true` - require login for the app, API, and uploads.
- `AUTH_PROVIDER=keycloak` - use OIDC/Keycloak signup and login.
- `APP_PUBLIC_URL=http://127.0.0.1:8000` - public URL used for OIDC callbacks and logout redirects.
- `OIDC_ISSUER_URL=http://localhost:8081/realms/wh40k` - browser-visible OIDC issuer.
- `OIDC_INTERNAL_ISSUER_URL=http://keycloak:8080/realms/wh40k` - container-network issuer used to fetch tokens and signing keys.
- `OIDC_CLIENT_ID=wh40k-web` - OIDC client id.
- `OIDC_ADMIN_ROLE=wh40k-admin` - provider role that grants app admin access.
- `WH40K_SESSION_DAYS=30` - app cookie session lifetime after OIDC login.
- `WH40K_COOKIE_SECURE=true` - use this when serving only over HTTPS.

To run the Compose stack without auth during development:

```bash
WH40K_AUTH_ENABLED=false docker compose up --build
```

Standalone Python runs still default to auth disabled. If you use the legacy local password provider with `python run.py --auth` or `AUTH_PROVIDER=local WH40K_AUTH_ENABLED=true`, the first startup creates a temporary `admin` password in the logs:

```bash
docker compose logs web
```

In provider auth mode, users are created and managed in Keycloak. In local password mode, users are managed from `/admin`.

## How syncing works

The app tries to use `git` first. Later syncs use `git pull --ff-only`.

Warhammer 40,000 10th Edition:

```bash
git clone --depth 1 --branch main https://github.com/BSData/wh40k-10e.git
```

Kill Team:

```bash
git clone --depth 1 --branch master https://github.com/BSData/wh40k-killteam.git
```

Age of Sigmar 4th Edition:

```bash
git clone --depth 1 --branch main https://github.com/BSData/age-of-sigmar-4th.git
```

If `git` is not installed, the app downloads the matching branch zip from GitHub and unpacks it.

## API endpoints

Most read endpoints accept a `game_system` query parameter. Valid values are:

- `wh40k_10e`
- `kill_team`
- `age_of_sigmar_4e`

Main endpoints:

- `GET /` - web front end
- `GET /login` - login page when auth is enabled
- `GET /admin` - admin portal for setting the admin password, creating users, and manually syncing BSData
- `GET /api/game-systems` - available game systems
- `POST /api/sync/wh40k_10e` - clone/pull 40k BSData and import `.cat` files; admin-only when auth is enabled
- `POST /api/sync/kill_team` - clone/pull Kill Team BSData and import `.cat` files; admin-only when auth is enabled
- `POST /api/sync/age_of_sigmar_4e` - clone/pull Age of Sigmar BSData and import `.cat` files; admin-only when auth is enabled
- `GET /api/status?game_system=wh40k_10e` - database and import status
- `GET /api/factions?game_system=kill_team` - imported factions/teams
- `GET /api/factions?game_system=age_of_sigmar_4e` - imported factions/armies
- `GET /api/units?game_system=wh40k_10e&query=chaos%20lord` - search imported catalogue entries
- `GET /api/inventory?game_system=kill_team` - list inventory for the selected tab
- `GET /api/inventory?game_system=age_of_sigmar_4e` - list AoS inventory
- `POST /api/inventory` - add inventory item
- `PUT /api/inventory/{id}` - update inventory item
- `DELETE /api/inventory/{id}` - delete inventory item and its stored photos
- `PUT /api/inventory/{id}/copies/{copy_id}` - update one per-quantity copy box
- `POST /api/inventory/{id}/images` - upload a JPG, PNG, WebP, or GIF photo
- `POST /api/inventory/{id}/copies/{copy_id}/images` - upload a photo to one per-quantity copy
- `DELETE /api/images/{image_id}` - delete a photo
- `GET /api/export.csv?game_system=wh40k_10e` - export the selected tab as CSV
- `POST /api/import.csv?game_system=wh40k_10e` - import a CSV previously exported by this app

## Data model notes

`bsd_units` contains imported catalogue data, scoped by `game_system`, including `wargear_options_json` for weapon profiles and `model_composition_json` for model-specific unit composition discovered in the `.cat` XML. `inventory_items` contains your own collection, scoped by `game_system` and, when auth is enabled, `owner_user_id`. `inventory_copies` stores one child record per inventory quantity, including per-copy base number, wargear selections, location, notes, and photos. Inventory rows keep a snapshot of the unit name and faction/team so your collection remains readable even if a catalogue entry is renamed or removed in a later BSData update.

Uploaded images are stored in S3-compatible object storage. In local Compose this is MinIO; the app serves authenticated image requests back under `/uploads/...`.

## Importer notes

The original importer only accepted catalogue entries marked exactly as `type="unit"`. This version also imports `type="model"` entries, which helps with character entries such as Chaos Lords and with Kill Team operative-style entries. It still ignores entries marked as upgrades/wargear.

BSData catalogue XML can vary between factions and releases. The importer looks for weapon-style profiles, including nested entries and linked profiles/entry links. For Age of Sigmar, `- Library` catalogue suffixes are removed from faction names shown in the app. If no weapon list can be found for a row, the UI falls back to a free-text wargear notes box. This app is meant as a stock tracker, not a full army builder.

This project is not affiliated with Games Workshop, BattleScribe, New Recruit, or BSData.
