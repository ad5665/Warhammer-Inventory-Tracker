# Local Container Stack

Use this stack while migrating toward the hosted web/mobile architecture. It runs the FastAPI app with Postgres for application data, MinIO as the local S3-compatible object store, and Keycloak as the local OIDC/signup provider.

## Start

```bash
docker compose -f docker-compose.dev.yml pull web
docker compose -f docker-compose.dev.yml up
```

Open the app:

```text
http://127.0.0.1:8000
```

Create a user:

```text
http://127.0.0.1:8000/signup
```

Keycloak:

```text
http://localhost:8081
```

Default Keycloak admin login:

```text
username: admin
password: admin-password
```

MinIO console:

```text
http://127.0.0.1:9001
```

Default MinIO login:

```text
username: wh40k
password: wh40k-secret
```

Postgres listens on:

```text
postgresql://wh40k:wh40k@127.0.0.1:5432/wh40k
```

## What Is Real Today

The dev stack uses the target local services directly:

- `DATABASE_URL=postgresql://wh40k:wh40k@postgres:5432/wh40k`
- `WH40K_DB_SCHEMA=public`
- `STORAGE_BACKEND=s3`
- `S3_ENDPOINT_URL=http://minio:9000`
- `S3_BUCKET=wh40k-dev-uploads`
- `WH40K_AUTH_ENABLED=true`
- `AUTH_PROVIDER=keycloak`
- `APP_PUBLIC_URL=http://127.0.0.1:8000`
- `OIDC_ISSUER_URL=http://localhost:8081/realms/wh40k`
- `OIDC_INTERNAL_ISSUER_URL=http://keycloak:8080/realms/wh40k`
- `OIDC_CLIENT_ID=wh40k-web`

Uploaded photos are written to MinIO and served back through the app's `/uploads/...` routes. Inventory, catalogue imports, users, sessions, and image metadata are written to Postgres. BSData sync runs when the app starts and then at local midnight; `/app/data` remains for BSData clone/download working files.

Keycloak stores its data in the same Postgres container under the `keycloak` schema. The `keycloak-realm-init` service creates the `wh40k` realm from `infra/keycloak/wh40k-realm.json` when it is missing, with public registration enabled, and applies `infra/keycloak/wh40k-user-profile.json` so signup asks for username, email, and password only. The app validates OIDC JWTs and creates a local `auth_users` row on first valid login.

The login and registration screens use the `night-lords` Keycloak theme from `infra/keycloak/themes/night-lords`. Compose mounts that directory into `/opt/keycloak/themes`, and the realm initializer sets `loginTheme=night-lords` every time it runs so existing local realms pick up theme changes without a reset.

Cognito is intentionally not required for the default local loop. A real Cognito or Amplify sandbox can still be exercised separately when testing AWS-hosted provider integration.

## Web And Mobile Development

The dev stack allows browser and Expo dev origins by default:

```text
http://localhost:5173
http://127.0.0.1:5173
http://localhost:19006
http://127.0.0.1:19006
```

Override with:

```bash
WH40K_CORS_ORIGINS=http://localhost:5173,http://192.168.1.50:8081 \
docker compose -f docker-compose.dev.yml up
```

For a physical mobile device, point Expo at the machine's LAN address, for example:

```text
http://192.168.1.50:8000
```

## Reset Local State

```bash
docker compose -f docker-compose.dev.yml down -v
```

This deletes the dev BSData working volume, the Postgres volume, the Keycloak schema data, and the MinIO volume.

## Production Compose

`docker-compose.yml` runs the same service shape with the normal local volume names and local app build. Use `docker-compose.dev.yml` when you want the published dev app image, explicitly development-named volumes, and web/mobile CORS defaults. Set `WH40K_DEV_IMAGE` to test a different published image tag.
