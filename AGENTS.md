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

## Tests & conventions

- Run: `python -m unittest discover -s tests` (322 tests, some skipped).
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