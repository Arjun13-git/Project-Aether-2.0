# apps/web — Aether workspace (Next.js)

UI only. It holds no business logic, no measurement maths and no secrets. Every number it shows comes from an API Measurement object.

- `pnpm dev` serves http://127.0.0.1:3000 and proxies `/api` to the FastAPI process on 127.0.0.1:8000.
- `pnpm gen:api` regenerates `src/lib/api-types.ts` from the API's OpenAPI document.
- `pnpm typecheck` and `pnpm lint` run the type and lint checks.

See `docs/LOCAL-DEV.md` and `docs/phase-1/ARCHITECTURE.md` at the repository root.
