# Web And Mobile Sync Scope

Branch: `web-mobile-sync-scope`

## Goal

Move the stock tracker from a single FastAPI/vanilla JS app to a hosted, multi-user system with self-service signup, a web app, and a mobile app that stay in sync across devices.

The target product should support:

- Public signup, email verification, password reset, and normal login/logout flows.
- Private user inventory by default.
- Shared global BSData catalogue imports for 40k, Kill Team, and Age of Sigmar.
- Synced inventory, per-copy data, photos, wargear selections, notes, and CSV export.
- Web and iOS/Android clients against the same API.
- A migration path from the current single-backend UI/API into separate web and mobile clients.

## Migration Progress

Started on this branch:

- Added stable `public_id` values for catalogue units, inventory rows, inventory copies, and images.
- Added `version` and `deleted_at` metadata for sync/conflict foundations.
- Switched inventory/image deletion to soft-delete metadata while keeping current UI behavior.
- Added `storage_key` metadata for the later object-storage migration.
- Added CORS origin configuration through `WH40K_CORS_ORIGINS`.
- Added an OpenAPI export script and placeholder TypeScript API-client package.
- Replaced the runtime database with Postgres via `psycopg`.
- Replaced local upload files with S3-compatible object storage via MinIO in local Compose.
- Updated Docker Compose so the app runs against Postgres, MinIO, and Keycloak by default.
- Added Keycloak-backed OIDC login, self-service signup, JWT validation, and first-login local user provisioning.

## Current State

The current app is a good single-backend prototype:

- `app/main.py` owns API routes, server-rendered auth pages, static frontend serving, upload endpoints, CSV export, and local session auth.
- `app/db.py` owns Postgres schema setup and lightweight migrations.
- `app/storage.py` owns S3-compatible object storage access.
- `app/bsdata.py` owns BSData clone/download, catalogue parsing, and import logic.
- `app/static/` is a vanilla browser app.
- `/app/data` is now BSData working state; Postgres, Keycloak, and object storage hold shared app state.

Important existing strengths:

- The Python BSData parser is already substantial and should be kept.
- FastAPI already exposes the main domain API.
- Inventory rows are already scoped by `owner_user_id` when auth is enabled.
- OIDC bearer auth is now supported for web/mobile clients, with app-session cookies still issued for the current browser UI.
- Tests already cover parser, DB, API, and summary behavior.

Important limits for the target product:

- Browser UI is tightly coupled to the FastAPI static app.
- The current browser UI still uses app-session cookies after provider login.
- Provider identities are mapped to the existing integer `auth_users.id`; the long-term UUID owner model is not finished yet.
- There is no offline conflict model beyond last write to the server.

## Recommendation

Keep FastAPI/Python as the backend and move the shared state out of the local filesystem.

Recommended target architecture:

```text
apps/api
  FastAPI backend
  BSData sync/import worker
  OpenAPI contract

apps/web
  React web app
  Hosted as static or SSR frontend

apps/mobile
  Expo React Native app
  iOS and Android builds

packages/api-client
  Generated TypeScript client from FastAPI OpenAPI
  Shared by web and mobile

Cloud services
  Managed auth with self-service signup
  Postgres for app data
  S3-compatible object storage for photos
  Scheduled/background job for BSData sync
```

This keeps the part of the codebase that is hardest to replace, the Python BSData importer, while allowing web and mobile clients to share one typed API.

Local development now uses Keycloak as the managed-auth stand-in. Hosted production can still use Cognito if Amplify remains the target, or another OIDC provider if we choose a non-AWS route.

## Amplify Fit

Amplify is useful, but it should be used selectively unless we choose a full AWS rewrite.

Good Amplify fits:

- Cognito-backed auth with self-service signup, verification, reset, and hosted UI/custom UI support.
- S3-backed photo storage.
- Hosting the React web frontend.
- Connecting React Native clients to Cognito/S3.

Poor initial fit:

- Replacing the whole backend with Amplify Data/AppSync/DynamoDB. The current domain has relational inventory/copy/image tables, CSV export, BSData import jobs, and Python XML parsing. Moving all of that to AppSync/Lambda/DynamoDB would be a larger rewrite than the web/mobile goal requires.

Preferred AWS option:

```text
React web on Amplify Hosting
Expo mobile using Amplify/Cognito libraries
FastAPI backend validates Cognito JWTs
Postgres on RDS/Aurora/Supabase/Neon-style managed Postgres
Images in S3
BSData sync as a protected API job or scheduled worker
```

Alternative non-AWS option:

```text
React web on Vercel/Netlify
Expo mobile
FastAPI on Fly.io/Render/Railway/ECS
Postgres managed by the host
Images in S3/R2
Auth via Auth0/Clerk/Supabase Auth
```

## Identity Model

Replace local password/session ownership with provider identities. This has started with Keycloak-backed identities mapped into the existing `auth_users` table.

Proposed tables:

- `users`
  - `id` UUID primary key
  - `auth_provider` text, for example `keycloak` locally or `cognito` in AWS
  - `auth_subject` text unique, the provider user id
  - `email` text
  - `display_name` text nullable
  - `created_at`, `updated_at`
- Keep owner scoping on inventory records, but move from integer `owner_user_id` toward `owner_id` UUID.

Initial tenancy:

- Each signed-up user owns their own inventory.
- Catalogue data remains global and read-only for normal users.
- Admin-only operations include BSData sync and possibly user support actions.

Future tenancy:

- Add `households` or `collections` if users need shared inventory between family/friends.
- Add role membership per shared collection.

## Data Store

Postgres is now the runtime persistence layer. The next data-store work is to formalize migrations and move integer ownership toward provider-backed user identities.

Pragmatic migration path:

1. Introduce Alembic migrations before releasing mobile clients.
2. Keep the current table concepts: `bsd_units`, `inventory_items`, `inventory_copies`, `inventory_images`, `import_runs`.
3. Continue using UUID external IDs for API/mobile sync:
   - `public_id` on inventory rows, copies, images, and catalogue units.
   - `owner_id` UUID on user-owned rows.
   - `deleted_at` for sync-friendly soft deletes.
   - `version` integer or `updated_at` precondition for conflict detection.
4. Replace direct schema setup with explicit migrations once the new web/mobile API stabilizes.

## File Storage

Images now live in S3-compatible object storage.

Recommended approach:

- Store image metadata in `inventory_images`.
- Store bytes in S3-compatible storage under an owner-scoped prefix:
  - `users/{owner_id}/inventory/{inventory_public_id}/{image_public_id}.{ext}`
- Serve images through signed URLs or backend-authenticated proxy URLs. The current implementation uses backend-authenticated proxy URLs.
- Keep upload limits and content-type validation from the current app.

Next step:

- Change object keys from the current inventory-public-id prefix to an owner-scoped prefix now that provider-backed users exist.

## Auth And Signup

Self-service signup should be handled by a managed provider rather than custom password/email code.

Current implementation:

- Local Compose uses Keycloak with public registration enabled.
- `/signup` sends users to Keycloak's registration page.
- FastAPI validates Keycloak JWTs for bearer-token API calls.
- On first valid provider login, FastAPI creates or updates the local `auth_users` record and scopes inventory through that local id.

Minimum flows:

- Sign up with email/password.
- Confirm email.
- Sign in.
- Forgot password/reset password.
- Sign out.
- Delete account or request deletion.

Backend behavior:

- API receives bearer JWTs from web/mobile.
- FastAPI middleware validates issuer, audience, expiry, and signature.
- Backend creates a local `users` row on first valid login if one does not exist.
- All user-owned queries filter by the local user id.

Current local password auth remains behind `AUTH_PROVIDER=local` as a temporary migration fallback.

## Sync Model

MVP sync should be online-first:

- Central Postgres is source of truth.
- Web and mobile fetch from the API.
- Clients use local caches for performance.
- Writes go directly to the API.
- Server returns updated records with `version` and `updated_at`.

Conflict handling for MVP:

- Use optimistic UI on the clients.
- Require `version` or `updated_at` preconditions on updates.
- If a stale client writes, return `409 Conflict` with the current server record.
- Client presents a simple "reload latest" path first.

Offline support can be phase two:

- Mobile stores pending mutations locally.
- Mutations include stable client IDs.
- Server deduplicates by `client_mutation_id`.
- Conflicts are resolved per record, not by trying to merge whole inventories.

## API Contract

Keep FastAPI as the API owner and make the contract mobile-friendly.

Needed API changes:

- Use bearer auth in addition to or instead of cookies.
- Ensure all user-owned resources have stable public IDs.
- Avoid returning local filesystem paths.
- Add paginated endpoints before large catalogues become a mobile problem.
- Add OpenAPI client generation for TypeScript.
- Add CORS configuration for hosted web and local mobile dev.

Candidate endpoint groups:

- `/api/me`
- `/api/game-systems`
- `/api/catalogue/status`
- `/api/catalogue/units`
- `/api/inventory`
- `/api/inventory/{public_id}/copies`
- `/api/images/upload-url` or authenticated multipart upload endpoints
- `/api/export.csv`

## Frontend Plan

React web:

- Start with Vite React or Next.js.
- Use TypeScript.
- Generate an API client from FastAPI OpenAPI.
- Use TanStack Query or equivalent for cache, loading, retry, and mutation state.
- Port existing `app/static/app.js` workflows screen by screen.

Expo mobile:

- Use Expo React Native with TypeScript.
- Share the generated API client and validation/domain helpers with web.
- Do not assume full UI component sharing between web and native at first.
- Share auth setup, API hooks, inventory formatting, and wargear summary logic where practical.

Suggested repo layout:

```text
app/
  existing FastAPI code during migration
apps/
  api/
  web/
  mobile/
packages/
  api-client/
  domain/
```

The repo can move gradually. It does not need to become a monorepo in the first PR unless the React/Expo scaffolding is being added immediately.

## BSData Sync

Keep BSData sync on the backend.

Reasons:

- Mobile apps should not clone GitHub repos or parse catalogue XML.
- Global catalogue imports should be shared across users.
- Sync can be protected, scheduled, retried, and logged centrally.

Needed changes:

- Move sync/import runs to a background job or admin-only protected endpoint.
- Consider scheduled refresh once hosting is in place.
- Keep import results global by `game_system`.
- Keep user inventory snapshots resilient to catalogue renames/removals.

## Migration Phases

### Phase 1: API Hardening

- Add typed API response models where missing.
- Add stable public IDs to inventory, copies, images, and units.
- Add pagination to catalogue search.
- Add CORS settings.
- Add OpenAPI client generation proof of concept.

### Phase 2: Hosted Auth

- Choose hosted auth provider, likely Cognito if using Amplify.
- Add JWT validation middleware. Done locally with Keycloak.
- Add local `users` table linked to provider subject. Started by extending `auth_users`.
- Add self-service signup in a small React auth screen or provider hosted UI. Done locally with Keycloak hosted registration.
- Keep admin-only local auth only as a temporary migration fallback.

### Phase 3: Postgres And Object Storage

- Introduce Alembic migrations.
- Port runtime schema to Postgres. Done for the current app.
- Add S3-compatible storage abstraction. Done for the current app.
- Add data import tooling only if we decide to carry forward pre-Postgres local data.

### Phase 4: React Web

- Scaffold React web app.
- Port current inventory/catalogue flows.
- Use the generated API client.
- Replace static `app/static` frontend after parity.

### Phase 5: Expo Mobile

- Scaffold Expo app.
- Implement auth, inventory list, add/edit item, copy editing, image upload, and search.
- Use the same API client and domain helpers.
- Start online-first; add offline queue only after core flows are stable.

### Phase 6: Production Deployment

- Add environments for dev/staging/prod.
- Add secrets management.
- Add database backups.
- Add object storage lifecycle/backups.
- Add CI for API tests, web tests, mobile type checks, and migrations.

## Risks

- Rewriting the frontend and backend at the same time would slow delivery. Keep the backend first.
- Mobile offline sync can become a project by itself. Ship online-first sync before offline mutation queues.
- Full Amplify/AppSync/DynamoDB would be a larger rewrite than needed because the existing app has relational data and Python import logic.
- Image storage migration needs careful ownership checks so users cannot read another user's uploads.
- App store releases make API stability more important than it is for a local web app.

## Immediate Next Tasks

1. Decide production auth provider: Cognito/Amplify versus another OIDC provider.
2. Decide hosted Postgres provider.
3. Introduce explicit Postgres migrations with Alembic.
4. Move image object keys to an owner-scoped prefix.
5. Create a generated TypeScript API client proof of concept.
6. Scaffold React web only after the API contract is stable.
7. Scaffold Expo mobile after auth and API client shape are proven.
