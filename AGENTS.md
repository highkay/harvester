# AGENTS.md — Harvester Project Memory

> Agent-facing knowledge for working in this repo. Read before modifying code.

## What this project is

GitHub-scanning AI-key harvester + FastAPI web control plane. Searches GitHub
for leaked AI-provider API keys, validates them against the provider's live API,
writes per-provider result files (`valid-keys.txt` etc.), and pushes validated
keys to configured targets (gpt-load / TavilyProxyManager / local token store).

- CLI mode: `python main.py -c <config.yaml>`
- Web mode: `web_main.py` (FastAPI on `:8000`) / `Dockerfile.web`

## Deployment topology (IMPORTANT — do not forget)

**Production is deployed on the `fnos` NAS, NOT on this workstation.**

| Environment | Where | URL / Port | Repo dir | Notes |
|---|---|---|---|---|
| **Production** | `admin@fnos` (SSH) | `http://<fnos-ip>:8002` | `/home/admin/harvester` | docker compose, data lives in `./data`, DB `data/harvester.db` (has 5 tokens, 40 push_logs) |
| **Dev / backup** | this workstation (Windows, `F:\git\harvester`) | `http://127.0.0.1:8000` | `F:\git\harvester` | local docker compose from this repo |

Production container: `harvester-web` (image built locally on fnos via
`docker compose up -d --build`). fnos git state is often **ahead of
`origin/main` by local commits** (verify with `git status` before assuming).
fnos has local edits to `Dockerfile.web` / `docker-compose.yml` and a
`docker-compose.yml.bak.20260812` backup — do not clobber those blindly.

### How to deploy to production (fnos)

```bash
ssh admin@fnos
cd /home/admin/harvester
git fetch origin && git log --oneline origin/main..HEAD   # review local-only commits first
git pull origin main                                       # then bring in remote
docker compose up -d --build                               # rebuild + restart
curl -s http://localhost:8002/health                       # expect {"status":"ok"}
```

Before pulling, review fnos-local commits (`git log origin/main..HEAD`) so the
update preserves intentional local changes (reverts, port/volume tweaks).

### Env / keys on production

- `.env` on fnos holds `WEB_AUTH_KEY`, `ENCRYPTION_KEY`, gpt-load/tavily keys.
- `ENCRYPTION_KEY` is the AES master key — rotating it makes stored tokens
  undecryptable. Back it up.
- Self-bootstrap kill-switch: `HARVESTER_SELF_BOOTSTRAP` (default `1` = on).
  NOTE: fnos container currently does NOT pass this env var (pre-github-feature
  compose file) — the github self-bootstrap feature must be deployed to fnos
  before it takes effect there.

## Feature: github self-bootstrap (provider "github")

- `provider/github.py` — `GitHubTokenProvider`, validates tokens via
  `GET https://api.github.com/user` (Bearer). Registered as `"github"`.
- `examples/config-github.yaml` — scan preset (workspace `./data`, task name
  `github`, key_pattern for `ghp_/gho_/ghu_/ghs_/ghr_/github_pat_`).
- `web/self_bootstrap_push.py` — `SelfBootstrapPushService`: after a `github`
  scan, reads `valid-keys.txt`, filters GH-prefixed keys, inserts them
  encrypted into the local `github_tokens` table (dedup by `token_hash`,
  label `harvester-bootstrap`), then hot-reloads credentials. Never raises.
- Triggered from `web/runner.py` `_on_completed` when `provider_name == "github"`.
- Default schedule seeded: `github 50 */6 * * *` (in `web/scheduler.py`).
- **Bootstrap requirement**: at least one seed GitHub token must exist in the
  token store first (`POST /api/tokens`), else scans fail at the runner gate
  ("No enabled API tokens found").

## Feature: hf search backend (search_type "hf")

- `search/hf.py` — unauthenticated HuggingFace Hub backend: dataset-name
  keyword search → recursive tree enumeration → text-ish file whitelist
  filter → raw `resolve/main/` URLs handed to the GATHER stage. Self-throttled
  (0.7 s/req), TTL discovery cache (600 s).
- Dispatch: `search/client.py` `search_with_count` routes `search_type == "hf"`
  before the GitHub path (GitHub behavior untouched). `config/schemas.py`
  `ALLOWED_SEARCH_TYPES` includes `"hf"`.
- `stage/definition.py` SearchStage: hf branches run BEFORE the GitHub
  credential gate (the source is anonymous).
- Preset: `examples/config-hf.yaml`; tests: `tests/test_search_hf.py`.
- Known limits: tree first page only (Link-header cursor not followed);
  whitelist = text suffixes + key-ish filename hints (binary junk filtered);
  dataset-name matching is noisy. Measured yield ≈ 0 real keys — keep as a
  low-frequency secondary source; do NOT invest further in it.

## Data-source exploration findings (2026-08, evidence-backed)

- **Staleness law**: leaked AI keys get revoked within hours (recurring repo
  fact: kimi gained 82 valid keys in a ~2.7 h window). Any SNAPSHOT code
  index (grep.app, Sourcegraph, searchcode) therefore yields ~0 valid keys —
  measured 2026-08: grep.app 143 candidates across 8 queries, 0 valid. Do not
  re-propose snapshot-index sources; yield comes from REALTIME search (GitHub
  API) × frequency, not from adding sources.
- **Telegram public-share channels** (`t.me/s/{channel}` HTML preview pages,
  no auth) are the only assessed source with GitHub-like freshness for
  publicly announced keys (mechanism proven by snscrape/aggregator OSS). Needs
  a curated channel list — NOT implemented.
- **Shodan (passive only)**: anonymous keyword search works (filters/paging
  require login). Fingerprint inventory works — measured: sub2api banner
  `Server: sub2api-reasoning-proxy/1.0`, new-api `405` panel responses.
  Plaintext tokens in indexed banners are RARE (`sk-ant-`: 1 global hit).
  NOT implemented.
- **Validation endpoint traps** (apply to any future provider work):
  - OpenRouter `GET /v1/models` is PUBLIC → false positives; use
    `GET /api/v1/key` (Bearer) as `provider/openrouter.py` does.
  - Gemini invalid keys return 400 — read the body error code.
  - Groq `/openai/v1/models` may not gate auth — follow with an authed
    chat probe.
  - SerpApi `search.json` answers 200 with real results even WITHOUT a key —
    validate only via `GET https://serpapi.com/account.json?api_key=`
    (free, quota-exempt; 401 = invalid). The account response echoes the
    `api_key` field — never store/log/inspect it.
- **Boundary (do not cross)**: default credentials documented in gateway
  projects (one-api/new-api `root/123456` panel lineage, CLIProxyAPI
  placeholders, sub2api auto-generated admin password) are "change before
  deploy" defaults, not public authorization. Do not build default-credential
  login/call against third-party instances; passive mapping (Shodan inventory
  records) and publicly announced keys only.

## Feature: serpapi provider (scan + validate + push)

- `provider/serpapi.py` — `SerpapiProvider` (registered as `"serpapi"`, mirror
  of `provider/tavily.py`). Validates via SerpApi Account API
  (`account.json?api_key=`); 401 → INVALID_KEY, 200 with account JSON →
  success (bare 200 without account fields → UNKNOWN, guarding against the
  search.json-style open endpoint). Keys are prefix-less 64-char hex — MEASURED on prod 2026-08-29: 395/395 valid keys are exactly 64 hex chars. The harness accepts 20-64 chars so validation stays length-independent; a fixed-32 pattern would truncate every real key (scans looked healthy, yielded nothing).
- Extraction (in `examples/config-serpapi.yaml` + `config/defaults.py`
  preset): context-anchored — env-name assignments (`SERPAPI_API_KEY` /
  `SERPAPI_KEY` / `SERP_API_KEY` / `serpapi[_-]?...key`) at task level, plus a
  domain-anchored variadic that adds query-string URLs
  (`?api_key=` / `&api_key=` / `&amp;api_key=`, any position in the URL).
  NO bare `[0-9a-f]{32}` AND no bare `api_key` branch — the first production
  scan proved a naked `api[_-]?key` branch floods the check stage (~84
  candidates/link, 55k total); env-name + URL forms only.
- Push: `web/serpapi_push.py` `SerpapiPushService` (mirror of
  `web/tavily_push.py`), env-gated by `SERPAPI_PROXY_BASE_URL` /
  `SERPAPI_PROXY_AUTH_KEY` — silent no-op until configured. Prod (fnos)
  configures them now: `SERPAPI_PROXY_BASE_URL=http://192.168.1.18:48081` +
  pool master key (see pool section). Contract identical to
  TavilyProxyManager: `POST {base}/api/keys`, Bearer, `{"key","alias":...}`;
  only 20–64-hex keys are pushed; one `push_logs` row.
- Hook: `web/runner.py` `_on_completed` fires serpapi push iff
  `provider_name == "serpapi"` (daemon thread, ImportError-safe).
- Schedule: `("serpapi", "10 */6 * * *", "examples/config-serpapi.yaml")` in
  `web/scheduler.py` — seeds only into an EMPTY `schedule_config` table, so
  prod needs a one-off row insert (or UI add) after deploy.
- Redaction: NO `tools/patterns.py` entry (bare hex would blitz logs); the
  provider never writes keys to disk/logs.
- Tests: `tests/test_serpapi_provider.py`, `tests/test_web_serpapi_push.py`,
  `tests/test_web_runner_serpapi.py`; scheduler expectations updated in
  `tests/test_web_scheduler.py`.

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

## Feature: groq provider — egress & honeypot facts (verified 2026-09-04)

- **Groq's Cloudflare edge 403-blocks fnos entirely.** Every request from
  fnos direct AND through the benchmarked socks trio
  (`192.168.1.18:1080/1090/1091`, whose egress is Cloudflare anycast
  `104.28.208.136`) gets bare `403 {"error":{"message":"Forbidden"}}` BEFORE
  key auth — even for a never-registered random key (normal clients get 401
  `invalid_api_key`). `provider/groq.py::_judge` maps 403+"forbidden" →
  INVALID_KEY, so a groq scan on fnos runs can NEVER produce a valid key,
  regardless of pool quality. This was the root cause of "0 valid since
  forever" (only 3 groq runs exist; schedule row added 2026-09-03).
- **Working egress**: `socks5://192.168.1.18:7890` (VPS relay, egress
  `107.172.141.203`) returns the normal 401 for invalid keys. Pin groq to it
  via `HARVESTER_PROXY_GROQ` in fnos `.env` — `web/runner.py::_pick_proxy`
  honors per-provider `HARVESTER_PROXY_<PROVIDER>` overrides (provider name
  upper-cased, punctuation → `_`), falling back to the global
  `HARVESTER_PROXY` rotation. The runner injects the picked proxy into the
  generated runtime YAML (comment in `_generate_temp_yaml` explains why).
- **Honeypot decoys**: repos poison the `"gsk_"` search space with fake keys
  whose bodies embed base64("XgroqX") == `WGdyb3FY` (measured: 44% of
  rejected candidates). `examples/config-groq.yaml` and the groq preset in
  `config/defaults.py` exclude them via a `(?!.*WGdy)` lookahead plus an
  alnum-only charset (drops GTK4 `gsk_*` symbols and doc placeholders).
  Length floor stays 20+ deliberately — the serpapi lesson: narrowing length
  without a measured real-key corpus silently drops every real key.
  Tripwires for future sessions: (1) "real keys are gsk_+48 alnum" is
  INFERRED from docs/decoys, not measured — measure the first real valid key
  and re-align only then; (2) if a hardened run yields near-zero candidates
  reaching the check stage, suspect the pattern first; (3) behaviour is
  pinned by tests/test_groq_pattern.py (decoy rejection, no capture groups,
  defaults↔examples lockstep).
- `run_records.total_keys_checked` is never written by `web/runner.py` (only
  `valid_keys_found`) — 0 there means "not wired", not "nothing checked".

## Feature: agnes-ai provider (scan + validate + push)

- `provider/agnes_ai.py`: `AgnesAIProvider` (registered as `"agnes-ai"`). Agnes
  AI is an OpenAI-compatible omni-modal gateway at
  `https://apihub.agnes-ai.com/v1`, authed with `sk-` Bearer keys. Validation
  is a minimal chat-completion probe: `POST /chat/completions` with
  `{"model":"agnes-2.5-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":1}`.
- **`GET /v1/models` trap**: the endpoint answers 200 for ANY Bearer token
  (presence-only, live-probed), so it is NOT used for validation; `inspect()`
  only lists model IDs for keys that already passed `check()`.
- Status map (`_judge_chat`): 200 + JSON → valid; 401 (or body `无效的令牌` /
  `invalid api key|token`) → INVALID_KEY; 402 → NO_QUOTA; 429 → RATE_LIMITED;
  403 → NO_ACCESS; 400 → BAD_REQUEST; >=500 → SERVER_ERROR; 200 non-JSON →
  UNKNOWN. Keys are `sk-` (length undocumented, no fixed-width pattern).
- Extraction (in `examples/config-agnes-ai.yaml` + `config/defaults.py`
  preset): context-anchored. Env-name assignments (`AGNES_API_KEY` /
  `AGNES_AI_API_KEY` / `AGNES_KEY` / `agnes[_-]?(ai[_-]?)?api[_-]?key`) at
  task level, with a negative lookahead excluding `sk-ant-` / `sk-proj-` /
  `sk-svcacct-`. The `apihub.agnes-ai.com` domain dorks
  (`"apihub.agnes-ai.com"`, plus `"Authorization"` and `"agnes-ai.com"
  "api_key"` variants) widen to a Bearer/quoted `sk-` pattern per condition.
- Push: `web/agnes_ai_push.py` `AgnesAIPushService` (mirror of
  `web/serpapi_push.py`), env-gated by `AGNES_LOAD_BASE_URL` /
  `AGNES_LOAD_GROUP_ID` / `AGNES_LOAD_AUTH_KEY`. After an agnes-ai scan it
  POSTs validated `sk-` keys to a gpt-load instance
  `POST {base}/api/keys/add-multiple` with body
  `{"group_id":<int>,"keys_text":"<key>\n<key>"}`, chunked at 500 keys per
  POST. Defaults: `AGNES_LOAD_BASE_URL=http://107.172.141.203:43001` (the
  user's gpt-load instance; 107.172.141.203 is the VPS public IP and LAN
  192.168.1.18 is the same host), `AGNES_LOAD_GROUP_ID=19`,
  `AGNES_LOAD_AUTH_KEY=""` (empty → no Authorization header; non-empty →
  `Bearer <key>`). Only generic `sk-` keys are pushed (`sk-ant-`/`sk-proj-`/
  `sk-svcacct-` excluded); never raises; idempotent per run_id; writes one
  `push_logs` row (gpt_load_config_id=0, group_id=env int).
- Hook: `web/runner.py` `_on_completed` fires agnes-ai push iff
  `provider_name == "agnes-ai"` (daemon thread, ImportError-safe).
- Schedule: `("agnes-ai", "35 */6 * * *", "examples/config-agnes-ai.yaml")`
  in `web/scheduler.py`; seeds only into an EMPTY `schedule_config` table, so
  prod (fnos) needs a one-off
  `INSERT INTO schedule_config (provider_name, cron, enabled, config_file) VALUES ('agnes-ai','35 */6 * * *',1,'examples/config-agnes-ai.yaml')`
  (or a UI add) after deploy.
- Redaction: NO `tools/patterns.py` entry (a bare `sk-` pattern would blitz
  logs); the provider never writes keys to disk/logs.

## Tests & conventions

- Run: `python -m unittest discover -s tests` (396 tests, 8 skipped as of
  2026-08-30; known env baseline = 35 failures in `test_web_ui` /
  `test_web_push_logs`; count grows — the historical "322" figure is stale).
- New files must pass `ruff check` and `pyright` (repo has pre-existing lint
  debt elsewhere — leave it).
- Provider pattern: mirror `provider/openrouter.py` / `provider/kimi.py`.
- Web push service pattern: mirror `web/tavily_push.py` (deliberate ~60-line
  helper duplication; do NOT extract a shared base class, do NOT change
  `web/push.py` gpt-load flow).
- Plans/evidence live under `.omo/plans/` and `.omo/evidence/` (gitignored;
  commit only code, not evidence).
- Commit style: `feat(provider):`, `feat(web):`, `fix(examples):`, `docs(web):`,
  one atomic commit per logical change.
- Credentials: never commit real tokens; use placeholders in examples.