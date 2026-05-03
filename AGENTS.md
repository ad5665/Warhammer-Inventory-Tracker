# Repository Guidelines

## Project Structure & Module Organization

This is a small FastAPI and SQLite application with a vanilla frontend.

- `app/main.py` defines the web app, API routes, auth flow, uploads, and CSV export.
- `app/db.py` owns SQLite setup, migrations, and persistence helpers.
- `app/bsdata.py` parses BSData catalogue XML into importable unit records.
- `app/static/` contains the browser UI: `index.html`, `app.js`, and `styles.css`.
- `tests/` contains pytest coverage for parser and summary behavior.
- `docs/screenshots/` stores README screenshots.
- Runtime data is local and untracked under `data/` or `/app/data` in Docker.

## Build, Test, and Development Commands

Create a local environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Install test and coverage tooling:

```bash
pip install -r requirements-dev.txt
```

Run the development server with reload at `http://127.0.0.1:8000`:

```bash
python run.py
```

Run with authentication enabled:

```bash
python run.py --auth
```

Run tests:

```bash
pytest
```

Run tests with the configured coverage report:

```bash
coverage run -m pytest
coverage report
```

Run with Docker Compose after creating the external volume once:

```bash
docker volume create wh40k-stock-data
docker compose up --build
```

## Coding Style & Naming Conventions

Use Python 3.11+ and follow existing PEP 8 style: 4-space indentation, snake_case functions and variables, and clear dataclass or dictionary field names that match API/database concepts. Keep FastAPI route handlers explicit and prefer small helpers in the same module unless logic is shared. Frontend code is plain JavaScript/CSS; use camelCase for JS variables and functions, kebab-case for CSS classes, and avoid introducing frontend build tooling unless needed.

## Testing Guidelines

Tests use pytest and live in `tests/` with names like `test_bsdata_parser.py` and `test_wargear_summary.py`. Add focused tests for parser changes, database migrations, auth behavior, API responses, and frontend-facing formatting rules. Prefer temporary files or directories via pytest fixtures, as existing parser tests do, rather than relying on local `data/` contents.

## Commit & Pull Request Guidelines

Recent commits use short imperative subject lines, for example `Hide inactive toast` and `Add legion theme selector`. Keep subjects concise and describe the user-visible or behavioral change. Pull requests should include a brief summary, test results (`pytest` or coverage output), linked issues when applicable, and screenshots for UI changes in `app/static/`.

## Security & Configuration Tips

Authentication is off by default. Use `WH40K_AUTH_ENABLED=true` for hosted deployments and `WH40K_COOKIE_SECURE=true` when serving only over HTTPS. Do not commit `data/stock_tracker.db`, uploaded images, temporary admin passwords, or cloned BSData catalogues.

## Unit Tests

- Use pytest
- Try and get 100% code coverage
- Create a separate test_x.py file for each python file 

## Commit messages
branchName: [what is in the commit]
example main: adding new feature x