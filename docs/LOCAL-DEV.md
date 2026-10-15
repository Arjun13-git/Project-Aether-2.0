# Local development (Phase 1)

Single-user, localhost only. There is no authentication; the API refuses to bind to a non-loopback address. No GPU is needed.

## Prerequisites

| Tool | Version used | Notes |
|---|---|---|
| Docker + Compose | 29.x | Runs PostGIS only |
| uv | 0.12 | Installs Python 3.13 and the locked dependencies |
| Node.js + pnpm | Node 26, pnpm 11 | Frontend |
| Network | HTTPS to `earth-search.aws.element84.com` and `e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com` | Live searches and window reads |

## 1. Database

```bash
cp infra/.env.example infra/.env                # set AETHER_DB_PASSWORD (git-ignored)
docker compose -f infra/compose.yaml up -d      # PostGIS 18-3.6 on 127.0.0.1:55432; creates DBs aether + aether_test
```

The database listens on loopback only. The PostGIS extension is created by the migration, not by the container.

## 2. API and worker

```bash
cd apps/api
cp .env.example .env            # use the password from infra/.env
uv sync                         # Python 3.13 venv from uv.lock
uv run alembic upgrade head     # Phase 1 schema (migrations/versions/0001_phase1_baseline.py)
uv run python -m aether.api.app       # API on http://127.0.0.1:8000  (OpenAPI: /docs)
uv run python -m aether.jobs.worker   # worker (separate terminal)
```

### Environment variables (`apps/api/.env`)

| Variable | Default | Meaning |
|---|---|---|
| `AETHER_DATABASE_URL` | — (required) | `postgresql+psycopg://aether:<pw>@127.0.0.1:55432/aether` |
| `AETHER_STORAGE_ROOT` | `./.data` | Content-addressed artifacts + window cache (git-ignored) |
| `AETHER_STAC_API_URL` | `https://earth-search.aws.element84.com/v1` | Must be https |
| `AETHER_STAC_COLLECTION` | `sentinel-2-c1-l2a` | Any other value is rejected (single collection, ADR-016) |
| `AETHER_BIND_HOST` | `127.0.0.1` | Loopback only |
| `AETHER_LOG_LEVEL` | `INFO` | Logs STAC searches, exclusions, reads, stages, timings; no arrays, no secrets |
| `AETHER_TEST_DATABASE_URL` | — | Enables the integration tests (the DB is dropped and re-migrated) |

## 3. Web

```bash
cd apps/web
pnpm install
pnpm dev                        # http://127.0.0.1:3000 (proxies /api → 127.0.0.1:8000)
                                # predev/prebuild copy the MapLibre worker into public/vendor/ (git-ignored)
pnpm gen:api                    # regenerate src/lib/api-types.ts from the running API's OpenAPI
```

## 4. Tests

```bash
cd apps/api
uv run pytest                                   # unit tests (no DB, no network)
AETHER_TEST_DATABASE_URL=… uv run pytest        # + integration tests against PostGIS
uv run ruff check src tests && uv run ruff format --check src tests
cd ../web && pnpm typecheck && pnpm lint
```

- `live`-marked tests are excluded by default.
- The end-to-end validation on real data runs through the public API:
  `uv run --project apps/api python scripts/demo_phase1.py --out results.json`.

## 5. Utilities

| Script | Purpose |
|---|---|
| `scripts/demo_phase1.py` | Runs the three civilian demo AOIs end to end through the API |
| `scripts/fixtures/fetch_s2_window.py` | Fetches a ≤ 256 px real window + provenance sidecar into the fixture tree (ADR-023) |
| `scripts/licence_report.py` | Licence tripwire over the installed Python and web dependencies |
| `scripts/repro_check.py` | Re-executes persisted runs from their recorded STAC JSON and compares artifact sha256 and statistics (I-12) |

## Storage layout (`AETHER_STORAGE_ROOT`)

```text
artifacts/{sha[:2]}/{sha256}.tif                 immutable, content-addressed COGs (evidence artifacts)
cache/windows/{grid_hash}/{item_id}/{asset}.npy  raw DN window cache + .json header metadata (disposable)
tmp/                                             in-progress writes
```

Deleting `cache/` is always safe. Artifacts referenced by evidence must be kept.
