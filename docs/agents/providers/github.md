# github self-bootstrap provider - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

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
