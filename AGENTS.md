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
fnos has local edits to `Dockerfile.web` /
`docker-compose.yml` and a `docker-compose.yml.bak.20260812` backup — do not
clobber those blindly.

**2026-09-22 deployment-drift warning**: after a day of scp + `docker compose cp`
deploys, the fnos working tree carries ~40 files as *uncommitted modifications*
whose content equals `origin/main` (the same changes were committed and pushed
from the workstation). Consequences: `git pull` refuses; and a naive
`git checkout -- .` / `git reset --hard` there would REVERT the deployed configs
to their pre-fix versions while the container keeps the new ones — a later
rebuild would then bake the OLD configs. To reconcile: back up `Dockerfile.web`
and `docker-compose.yml`, `git fetch origin`, `git reset --hard origin/main`,
restore `Dockerfile.web`, then `cp docker-compose.hostnet.yml docker-compose.yml`
(the running container is unaffected by file edits; only the next
recreate/build reads them).
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

> **2026-09-21 CORRECTION — the Cloudflare block described below is NO LONGER
> accurate.** Measured on fnos (container on host networking): the socks trio
> with `socks5://` (LOCAL DNS) returns the normal `401 invalid_api_key` (6/6
> probes); only `socks5h://` (remote DNS) still gets the bare 403. The
> dedicated relay `socks5://192.168.1.18:7890` is GONE (connection refused,
> nothing listening on the NAS). `HARVESTER_PROXY_GROQ` in fnos `.env` is now
> EMPTY so groq inherits the global trio. Re-verify before trusting either
> direction again.

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
- **"0 valid since forever" was ALSO a corpus-replay bug (fixed 2026-09-06).**
  `manager/task.py::_on_start` recovered the previous run's
  `links.txt`/`material.txt`/`invalid-keys.txt` (and shared `queue_state`
  files, provider-filtered) BEFORE backing them up, so every nightly groq
  run re-checked last night's pool instead of searching fresh — measured:
  consecutive links.txt sets were byte-identical (0 new/0 dropped, only
  row-order shuffle) while run_records said "completed". The egress/pattern
  fixes were necessary but unobservable under replay. Fix: `persistence.
  auto_restore` gate is now actually consumed by `_on_start` (it was a
  parsed-but-unused flag); `examples/config-groq.yaml` sets
  `auto_restore: false` (clean start each run, still backs up old files).
  Dorks widened too: `"gsk_"` plus `GROQ_API_KEY` env anchors,
  `api.groq.com` domain dorks, and a `created:>=2026-08-01` freshness window
  (max_pages 200); pattern excludes doc placeholders (marker words,
  abc123-style sequences, 4-char runs). Prod verification 2026-09-06:
  manager.log shows "clean start", 7 initial search tasks, fresh gather
  queue (5k+ links in 4 min), zero "Recovered" lines. Diagnosis recipe:
  set-diff two backups' links.txt + grep manager.log for
  "Recovered N unique links" vs "clean start". Fun fact: the 165-candidate
  pool that ran for 3h nightly was doc placeholders (gsk_xxx…/gsk_test…/
  gsk_abc123…) — all re-verified dead (163×401 + 2 network-EXH, 0 live).
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
- **Egress & DNS trap (measured on fnos 2026-09-04)**: `apihub.agnes-ai.com`
  DNS is polluted inside the container, so plain `socks5://` (local DNS) and
  container-direct both fail with ConnectionError/NETWORK_ERROR while
  `socks5h://` (remote DNS at the tunnel) returns 200. This silently poisoned
  the first 3 agnes runs (~110 candidates: every check was NETWORK_ERROR, yet
  landed in `invalid-keys.txt` — indistinguishable from real invalid keys
  until a REAL valid key was e2e-probed). Pin agnes to the socks5h rotation
  via `HARVESTER_PROXY_AGNES_AI` (compose default:
  `socks5h://192.168.1.18:1080,1090,1091`). `search/client.py` and
  `config/schemas.py` both whitelist `socks5h` (with mandatory port). The
  trap also cost 0 real keys in run 1-3; a live probed REAL key (`sk-` + 48
  alnum — measured 2026-09-04) returns 200 via socks5h from the container.
- Always measure real-key egress per PROVIDER (not just from the host) before
  trusting invalid counts — host-direct success does NOT imply container
  success (groq taught 403-blocks; agnes adds DNS pollution + socks5h).

## Feature: modelscope provider (scan + validate + push)

- `provider/modelscope.py`: `ModelScopeProvider` (registered as `"modelscope"`).
  Validation is `GET https://modelscope.cn/openapi/v1/users/me` with
  `Authorization: Bearer <token>` (single CN hub endpoint; clean status map).
- Status map (`_judge`): 200 + `success:true` + `data` dict → valid; 401 (or
  body `InvalidAuthentication`) → INVALID_KEY; 403 → NO_ACCESS; 429 →
  RATE_LIMITED; >=500 (post-retry) → NETWORK_ERROR; 200 without proper
  JSON/data → UNKNOWN (presence-only trap guard).
- Traps: `GET https://api-inference.modelscope.cn/v1/models` returns 200 for
  ANY key (presence-only) — never used for validation; `POST /api/v1/login`
  returns 400 + business Code 10010103009 for invalid tokens (not used as
  primary because of read-only-tier subtleties).
- Extraction: context-anchored — task-level env-name assignments capturing
  `ms-[A-Za-z0-9_-]{8,}` (no naked branch — the old bare `[0-9A-Za-z_-]{20,}`
  flooded the check stage with 2648+ candidates / 0 valid on 2026-08-11);
  domain-anchored conditions add Bearer/quoted variadic + `oauth2:(ms-…)@`
  git-URL form.
- Egress: verified 2026-09-06 from the fnos harvester container — direct
  connection to modelscope.cn works (no proxy, no DNS pollution);
  `use_proxy: false`.
- Tripwire: the `ms-` charset/length floor of 8 is from docs/third-party spans
  — measure the FIRST real valid key and re-align before narrowing; never add
  an upper bound (serpapi lesson).
- `inspect()` returns `[]` (hub token → user profile, no model enumeration).
- Push: `web/modelscope_push.py` `ModelScopePushService` (mirror of
  `web/agnes_ai_push.py`), env-gated by `MODELSCOPE_LOAD_BASE_URL` /
  `MODELSCOPE_LOAD_GROUP_ID` / `MODELSCOPE_LOAD_AUTH_KEY`. After a modelscope
  scan it POSTs validated keys to a gpt-load instance
  `POST {base}/api/keys/add-multiple` with body
  `{"group_id":<int>,"keys_text":"<key>\n<key>"}`. Defaults:
  `MODELSCOPE_LOAD_BASE_URL=http://192.168.1.18:43001` (the user's gpt-load
  instance; 192.168.1.18 is the fnos NAS LAN IP), `MODELSCOPE_LOAD_GROUP_ID=13`
  (group named "modelscope"), `MODELSCOPE_LOAD_AUTH_KEY=""` (empty → no
  Authorization header; non-empty → `Bearer <key>`); never raises; idempotent
  per run_id; writes one `push_logs` row (gpt_load_config_id=0, group_id=env
  int).
- Hook: `web/runner.py` `_on_completed` fires modelscope push iff
  `provider_name == "modelscope"` (daemon thread, ImportError-safe).
- Schedule: `("modelscope", "0 11 * * *", "examples/config-modelscope.yaml")`
  daily 11:00 in `web/scheduler.py`; seeds only into an EMPTY
  `schedule_config` table, so prod (fnos) needs a one-off row insert (or a UI
  add) after deploy.
- Redaction: NO `tools/patterns.py` entry (a bare `ms-` pattern would blitz
  logs); the provider never writes keys to disk/logs.

## Feature: glm + glm-ai providers (split + first yield run, 2026-09-21)

- **Key format is MEASURED, not inferred**: `<32 lowercase hex>.<16+ mixed-case
  alnum>`, 49 chars (sample live-validated at 200 on api.z.ai). The original
  pattern `[0-9a-f]{32}\.[0-9a-f]{32}` (32-hex secret) matched NO real key —
  scans looked healthy while extracting 0 candidates (serpapi lesson again).
  Fixed to `[0-9a-f]{32}\.[A-Za-z0-9]{16,}` in `examples/config-glm*.yaml`,
  `config/defaults.py`, `examples/config-full.yaml` and `tools/patterns.py`
  (the redaction pattern had the same bug and leaked real keys into logs).
- **Endpoints**: `glm` -> `https://open.bigmodel.cn/api/paas/v4` (direct);
  `glm-ai` -> `https://api.z.ai/api/paas/v4` (see the egress section). Both
  expose an auth-gated `GET /models` (the old "no /models endpoint" comment was
  wrong) but validation uses the chat-completions probe with `glm-5.3-flash`.
  **Policy 2026-09-21: the gpt-load `glm`/`zai` groups only accept
  glm-5.3-flash-capable keys** — free-tier flash-only keys answer 429/1113
  ("余额不足或无可用资源包") for every non-flash model and are classified
  no-quota (never valid, never pushed). Measurement that led here:
  `glm-4.5-flash` answered 200 on 10/10 CN probes (least congested),
  `glm-4.7-flash` was mostly 429 code 1305, and all non-flash ids returned
  1113; the operator decided the pool must serve `glm-5.3-flash` only, and the
  previously pushed 362 free-tier keys were cleared from both groups.
- **Status map**: 401 / body code 1000·1001·1003 -> invalid; 400 code 1211 ->
  NO_MODEL; 402 / code 1113 -> no-quota; **429 code 1305 (model overload) ->
  RATE_LIMITED -> `wait-check-keys.txt`** (62 occurrences in one 45-min window
  measured — expect a fat wait bucket on busy days; those keys are recoverable,
  not lost).
- **First production run (manual, 3h18m, ended by a service restart)**: `glm`
  234 valid / 164 invalid / 100 wait (60.8k links); `glm-ai` 25 valid / 38
  invalid / 27 wait (41.8k links). Pushed: `glm` -> gpt-load group 14
  (234/234/0), `glm-ai` -> group 15 (25/25/0, union of a `backup-<ts>/` dir +
  live file). `run_records.valid_keys_found` was NOT written (the run was
  reconciled as `failed: interrupted by service restart`) — authoritative
  counts live in the provider files + `push_logs`.
- **Config**: `auto_restore: false` in both (the first-run pool is ~60k links;
  replaying it daily would re-gather the whole backlog). Schedules: `glm`
  `0 12`, `glm-ai` `0 13` (daily, staggered).

## Feature: kimi-coding (corrected 2026-09-21)

- Endpoint `https://api.kimi.com/coding/v1`; auth-gated `GET /models` lists
  ONLY `kimi-for-coding, kimi-for-coding-highspeed, k3, k3-256k`.
  **`kimi-k3` is an Open-Platform (api.moonshot.cn) id and does NOT exist
  here** — the old `default_model: kimi-k3` made a live key answer 401 "model
  id does not exist", which was classified INVALID_KEY and silently discarded.
  Now `kimi-for-coding`; `provider/kimi.py` maps 401 model/plan errors
  (`model id does not exist|recognized as other|does not have access to`) to
  NO_MODEL so they land in wait-check instead.
- Error semantics (measured): 403 `access_terminated_error` "Your current
  subscription does not have access to Kimi Code right now" = authentic key
  with a lapsed/absent subscription -> NO_ACCESS -> `wait-check-keys.txt`
  (never pushed); 401 "The API Key appears to be invalid or may have expired"
  = dead/revoked key -> invalid. Real keys are `sk-kimi-` at 72 chars.
- The one key ever pushed (Aug) later 401'd on every probe — staleness law,
  not a misclassification.
- Pattern narrowed to `sk-kimi-[0-9A-Za-z_-]{20,}` (2026-09-21): the plain
  `sk-` pattern pulled ~300 dead platform/placeholder keys through the check
  stage every run for 0 valid keys; 16/16 real coding keys are `sk-kimi-` +
  72 chars, so only the prefixed form is extracted now. Tripwire: if a future
  run shows near-zero candidates at the check stage, widen back to `sk-` and
  re-measure before trusting the narrowing.

## Ops: container egress, host networking & deploys (2026-09-21)

- The harvester container now runs `network_mode: host` + `WEB_PORT=8002`
  (was bridge + `8002:8000`), because **container-direct requests to api.z.ai
  through docker NAT were ~50-80% lossy while host-direct was 5/5 fast**.
  Post-switch container-direct: 5/6 at 1.3-8.6s — the Aliyun-GA IPv6 addresses
  answer, the IPv4 addresses are blackholed (0/9 with IPv4 pinned). `glm-ai`
  runs `use_proxy: false`; watch its wait-check ratio and flip back to the
  socks5 rotation if it degrades.
  **The fnos container runs the repo's `docker-compose.hostnet.yml`** (kept in
  the repo for reproducibility; on the NAS it is copied over
  `docker-compose.yml`). The repo's default compose stays bridge networking, so
  a plain `git pull` + `docker compose up -d`/`--build` would REVERT prod to
  bridge (and re-break z.ai direct egress) — re-apply the hostnet file
  (`cp docker-compose.hostnet.yml docker-compose.yml`) when deploying.
- **groq**: see the 2026-09-21 correction in the groq section — trio+socks5 now
  works, socks5h is the one that 403s, and the 7890 relay is gone.
- **cerebras is Cloudflare-blocked on every tested path** (direct, socks5,
  socks5h, browser UA -> 403 + a CF support JSON). Its schedule is DISABLED
  until a working egress is found — do not re-enable blindly.
- **`docker compose up -d --build` is BROKEN on fnos**: the docker daemon's
  registry proxy (`127.0.0.1:7890`) is dead, so `python:3.12-slim` cannot be
  pulled. Working deploy recipe: `docker compose up -d` (recreate from the
  local image) -> `docker compose cp <changed files> harvester-web:/app/...`
  -> **`docker compose restart`** (a bare `cp` does not reload already-imported
  modules, and a recreate wipes the copied layer — the restart is what makes
  new code active without a rebuild).
- **Concurrency ceiling**: fnos is a 4-core box already at load ~8 with 6-8
  concurrent scans. Aggregate gather throughput measured ~855-953 links/min
  (~31/min per gather thread vs ~125 in a single run), so raising
  `pipeline.threads.gather` does NOT help — reduce overlap and per-run link
  volume instead.
- **`POST /api/runs/{id}/cancel` does not stop a mid-run scan** (the cancel
  event is only checked before `app.run()`). The only way to stop one is a
  container restart; startup reconciliation then records it as `failed:
  interrupted by service restart`.
- **`data/queue_state/*.json` is shared by every concurrent run** and gets
  overwritten in turn — never use its size as a per-run progress signal; read
  the per-run display tables from the container logs instead.
- **Per-exit-IP throttling hits ANY provider, not just groq (tavily measured
  2026-09-22)**: `api.tavily.com/usage` answered
  `429 {"detail":{"error":"Your request has been blocked due to excessive
  requests"}}` for **every** key once that exit's IP was throttled — measured:
  exit `1080` blocked (429 for a valid key AND a dead key in the same probe),
  exits `1090`/`1091` fine (valid key -> 200, dead key -> 401). Symptom is a
  wait bucket that balloons (a run showed ~2374 wait of ~2400 checks) so the
  pool looks "mostly dead" when it is mostly FALSE-wait; the earlier wait-pool
  recovery then salvages ~47% as valid. Fix = per-provider override:
  `HARVESTER_PROXY_TAVILY=socks5://192.168.1.18:1090,socks5://192.168.1.18:1091`
  (docker-compose passes it through; takes effect on the next restart). Lesson:
  when a provider's wait bucket explodes, A/B the three exits before blaming
  the keys.
  **These per-exit blocks are TEMPORARY and they ROTATE** (measured on
  2026-09-22: exit 1080 blocked at 10:30 -> healthy by 10:48; exit 1091 blocked
  by 11:00) **and bulk re-checking trips them**: a wait-pool recovery probing at
  ~1-2 s/key got exit 1090 blocked after ~100 keys. Pinning a provider to a
  fixed pair is therefore pointless — prefer inheriting the global trio (any
  exit can be blocked at run start) or re-probe all exits right before pinning,
  and keep bulk re-checks at >=5-6 s/key for tavily.
- **Tavily `/usage` 200 != usable (fixed 2026-09-22)**: `/usage` answers 200 for
  ANY authentic key, including plan-exhausted ones (those answer 402 on real
  `/search` calls), so exhausted keys were pooled as useless entries.
  `provider/tavily.py::_judge_usage` now classifies
  `account.plan_usage >= plan_limit` (unless paygo credits remain) or
  `key.usage >= key.limit` as NO_QUOTA — never valid, never pushed
  (`tests/test_tavily_provider.py`, 30 tests).
- **gpt-load test-model mismatch → "keys cannot be verified" (measured
  2026-09-21)**: the `glm`/`zai` groups had `test_model: glm-5.3-flash`, but the
  harvested free-tier keys answer **429 code 1113 ("余额不足或无可用资源包")**
  for every non-flash model, so gpt-load's verification failed 100% while the
  keys were perfectly usable for `glm-4.7-flash` / `glm-4.5-flash` (flaky
  ~1/3 of probes 200, the rest 429 code 1305 model congestion). The group's
  test model must match what the pooled keys can serve. Fix path:
  `PUT {gpt_load}/api/groups/{id}` with the full group object (auth
  `Bearer $GPT_LOAD_AUTH_KEY`) — `GET /api/groups` lists groups, and
  `PUT /api/groups` does NOT exist (404); the update is the id-suffixed route.
  Same latent trap found in group `kimicoding` (`test_model: k3`, plan-gated on
  low tiers) → now `kimi-for-coding`. Note: free flash models are congested, so
  even with the right test model expect intermittent validation failures —
  that is capacity, not key validity.

- **`persistence.auto_restore` defaults to FALSE since 2026-09-21.** It means
  "replay the previous run's accumulated links/invalid/valid pools" (crash /
  resume recovery). On a recurring schedule that replay RATCHETS the corpus:
  every run re-queues the whole backlog (measured: deepseek 238k->267k links
  over 9 days -> 869 min avg runs, max 3196 min; log proof:
  `Recovered 16623 unique links items from legacy file`). Defaults are pinned in
  three places (`config/schemas.py`, `config/loader.py`,
  `manager/task.py::_on_start`) and by
  `tests/test_task_manager_autorestore.py`. Use `auto_restore: true` ONLY for
  resume / validate-existing profiles (`config-nvidia-resume-existing.yaml`,
  `config-nvidia-validate-existing.yaml`,
  `config-tavily-validate-existing.yaml`). Old files are always backed up to
  `backup-<ts>/`, and the wait-check pool can be re-validated on demand (see
  the wait-pool recovery recipe below).

### Wait-pool recovery recipe (validated 2026-09-21)

`wait-check-keys.txt` is terminal for the pipeline (never automatically
re-checked), but many entries are recoverable — keys that hit 429/1305 model
congestion, or a model-id mismatch, rather than being dead. Recipe: read the
wait file, re-check each key against the provider endpoint with the CURRENT
model at a throttled rate (**5 s/key**; hammering triggers `429 code 1302`
account rate limits, and 1 s/key on 2.6k keys is also needlessly slow), bucket
by the provider's own semantics (200 -> valid, 401 -> invalid, 1113/no-quota ->
no-quota, everything else stays in wait), rewrite the result files (dedupe-union),
then **push immediately** — the next clean-start run resets the files, so
recovered keys must not wait for the run cycle. Measured 2026-09-21: glm
100 -> 5 wait (**+95 valid**), glm-ai 27 -> 4 (**+23**), tavily 2665 wait
(mostly dead 401s, ~20% salvageable). Caveats (tavily, 2026-09-22): keep the
rate at >=5 s/key — faster probing trips the per-exit IP throttle (429 "blocked
due to excessive requests") and the bucket then refills with FALSE-wait; and a
resumable progress file must skip only TERMINAL buckets (valid/invalid) so its
own wait entries are re-checked on the next pass. Recovered value is often
smaller than it looks: of 1281 recovered tavily "valid" keys, 1059 were already
in the proxy pool (only 222 were new).

## Tests & conventions

- Run: `python -m unittest discover -s tests` (490 tests, 8 skipped, 0 failures
  as of 2026-09-21 — the old "35 failures in test_web_ui / test_web_push_logs"
  baseline is stale; the suite is green now).
- **One config file = one task.** Do NOT bundle regional tasks into one config
  (2026-09-21: `glm`/`kimi`/`mimo`/`qwen` were split into per-task files). A
  bundled run aggregates 2-3 tasks into one `run_records.valid_keys_found` and
  mixes statistics; pushes are per task anyway. `web/scheduler.py` now seeds
  one schedule row per task (15 rows, daily staggered).
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