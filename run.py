import argparse
import os

import uvicorn


def env_port(default: int = 8000) -> int:
    raw_value = os.getenv("WH40K_PORT") or os.getenv("PORT")
    if raw_value is None:
        return default
    try:
        port = int(raw_value)
    except ValueError as exc:
        raise SystemExit(f"Invalid port value: {raw_value!r}") from exc
    if not 1 <= port <= 65535:
        raise SystemExit(f"Port must be between 1 and 65535: {port}")
    return port


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Warhammer Stock Tracker dev server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=env_port())
    parser.add_argument("--auth", action="store_true", help="Require username/password login.")
    args = parser.parse_args()

    if args.auth:
        os.environ["WH40K_AUTH_ENABLED"] = "true"

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=True)
