# serpapi_proxy key-pool service - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Feature: serpapi_proxy — SerpApi key pool service

- **Canonical repo: https://github.com/highkay/serpapi_proxy** (extracted
  2026-08-30). The in-tree `serpapi_proxy/` dir below is a legacy snapshot —
  do NOT edit it; send changes to the standalone repo. It imports NOTHING
  from the harvester packages (image copies only `serpapi_proxy/`). Stdlib
  sqlite3 store + FastAPI app + optional quota-refresher daemon thread.
- API: `GET /healthz` (no auth); everything else requires
  `Authorization: Bearer $MASTER_KEY`. Admin: `POST /api/keys` (200 added /
  400 `create_failed`|`invalid_key_format` / 401), `GET /api/keys` (masked
  `key[:6]…key[-4:]` — raw keys never in responses), `DELETE
  /api/keys/{id}`, `POST /api/keys/{id}/refresh`, `GET /` HTML status page.
  Catch-all `GET /{path}` is a transparent rotating proxy: picks best key
  (unknown quota → most searches_left → LRU), injects `api_key`, retries up
  to 3 across 401(→invalid)/429(→60s cooldown)/ConnectionError(→10s
  cooldown), 4xx passthrough, exhausted pool → 503 `no_available_keys`.
- Auth gate is an ASGI middleware, NOT FastAPI route dependencies — FastAPI
  ≥0.116 ignores dependency-returned Responses (measured 0.128) and the
  whole pool would have been open. Keep `_require_bearer` as middleware.
- Duplicate POST returns 400 `create_failed` both via find-before-add and
  via catching `sqlite3.IntegrityError` from the find→add race window.
- Per-POST /api/keys the pool synchronously validates the key against
  serpapi.com account.json (up to `timeout`s) before returning 200 — so
  harvesters pushing hundreds of NEW keys need a long CLI timeout (~8 min
  for 410 keys measured 2026-08-30; duplicate re-pushes are ~5 s since the
  dup path skips the account check).
- Tests: `python -m unittest discover -s serpapi_proxy/tests -t .` (27).
- **Deployed on fnos prod from the standalone repo** (container
  `serpapi-proxy`, host port 48081): clone at `/home/admin/serpapi_proxy`,
  data `/home/admin/serpapi_proxy/data/pool.db` (migrated from the old
  harvester-tree deploy, which is now `docker compose down`). fnos LAN IP
  is **192.168.1.18** (192.168.1.11 is the rq host, NOT the NAS) → harvester
  `.env` holds `SERPAPI_PROXY_BASE_URL=http://192.168.1.18:48081`.
- MASTER_KEY lives in `/home/admin/serpapi_proxy/.env`; harvester `.env`
  `SERPAPI_PROXY_AUTH_KEY` MUST match. Rotate = rewrite both, then
  `docker compose up -d` in each project. The standalone Dockerfile takes a
  `PIP_INDEX_URL` build arg — fnos `.env` sets the tuna mirror.
- e2e proof: push_logs `pool-e2e-1` = `success|410|410|0`;
  `pool-e2e-2` (after extraction) = `success|420|0|420`; pool 420 rows
  (229 active / 191 exhausted); forward `search.json?engine=google…` → 200
  Success.
