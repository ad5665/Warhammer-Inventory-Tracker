# API Client

This package is the placeholder for the generated TypeScript client shared by the future React web app and Expo mobile app.

Generate the OpenAPI schema from the FastAPI app:

```bash
python scripts/export_openapi.py packages/api-client/openapi.json
```

Then generate TypeScript types with `openapi-typescript`:

```bash
npx openapi-typescript packages/api-client/openapi.json -o packages/api-client/src/schema.ts
```

The generated files are intentionally separate from the current vanilla frontend so the migration can happen without breaking the existing app.
