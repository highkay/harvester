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
- **Boundary (do not cross)**: default credentials documented in gateway
  projects (one-api/new-api `root/123456` panel lineage, CLIProxyAPI
  placeholders, sub2api auto-generated admin password) are "change before
  deploy" defaults, not public authorization. Do not build default-credential
  login/call against third-party instances; passive mapping (Shodan inventory
  records) and publicly announced keys only.

## Tests & conventions

- Run: `python -m unittest discover -s tests` (351 tests, 8 skipped; count
  grows — the historical "322" figure is stale).
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