# AGENTS.md — Harvester Project Memory

> Agent-facing knowledge for working in this repo. Read before modifying code.
>
> **Split on 2026-10-22.** This file used to be 216 KiB, so the harness truncated it
> at 64 KiB and silently dropped ~70% of the memory. Provider-, search- and
> audit-specific detail now lives verbatim under `docs/agents/` (loaded on demand
> through the map below); what stays here is the always-on core.
>
> Writing rule: area-specific knowledge goes into the matching `docs/agents/` file
> (append a dated section; never delete history). Only cross-cutting rules that
> every session must know belong in this file - keep it under 60 KiB (hard limit
> 64 KiB per context file), and add an index row when you create a new doc.

## Knowledge map - read the matching doc before working in that area

These files are NOT loaded automatically; open the one that matches your task.
The same table lives in `docs/agents/README.md`.

| Area | Doc | Read before |
|---|---|---|
| Provider: github self-bootstrap | `docs/agents/providers/github.md` | touching `provider/github.py`, `web/self_bootstrap_push.py`, `examples/config-github.yaml` or GitHub-token scans |
| Search: hf backend | `docs/agents/search.md` | adding or debugging a non-GitHub search backend / `search/client.py` dispatch |
| Provider: serpapi | `docs/agents/providers/serpapi.md` | changing serpapi extraction (env-name + URL anchoring, no bare hex), validation (`account.json`) or push |
| Service: serpapi_proxy pool | `docs/agents/serpapi-proxy.md` | touching the standalone serpapi_proxy service or its API contract |
| Provider: groq | `docs/agents/providers/groq.md` | any groq scan: egress 403 history, `WGdyb3FY` decoys, socks5-vs-socks5h correction |
| Provider: agnes-ai | `docs/agents/providers/agnes-ai.md` | agnes-ai scans/pushes: socks5h DNS requirement, `/v1/models` presence-only trap, gpt-load group 19 |
| Provider: modelscope | `docs/agents/providers/modelscope.md` | modelscope scans/pushes: `users/me` validation, presence-only `/v1/models`, gpt-load group 13 |
| Provider: glm / glm-ai | `docs/agents/providers/glm.md` | glm work: measured `<32hex>.<16+>` format, probe-model policy (glm-4.5-flash), 429/1305 wait routing |
| Provider: kimi family | `docs/agents/providers/kimi.md` | kimi / kimi-ai / kimi-coding work: env-anchored `sk-` extraction, 48-char corpus, 401 model-id mapping |
| Provider: ollama | `docs/agents/providers/ollama.md` | ollama work: why 0 new keys, GitHub code-search semantics, `/v1/models` is public, gpt-load wedge |
| Provider: tavily (+ hikari push) | `docs/agents/providers/tavily.md` | tavily work: `/usage` 200 is not usable, per-exit bulk 429 blocks, wait-pool recovery, hikari target |
| Provider: brave | `docs/agents/providers/brave.md` | brave scans/validation/push or its config preset |
| Provider: commandcode | `docs/agents/providers/commandcode.md` | commandcode scans/validation/push: `/provider/v1/models` public trap (identical 18,461-byte 200), chat-probe judge, undetermined key shape |
| Provider: clinepass | `docs/agents/providers/clinepass.md` | clinepass scans/validation/push: `/api/v1/models` public trap (identical 41,064-byte 200), chat-probe judge, probe id from public recommended-models catalog |
| Provider: siliconflow | `docs/agents/providers/siliconflow.md` | siliconflow scans/validation/push: `sk-`+48-lowercase corpus shape, clean `/v1/models` 401 gate, 404/20012→NO_MODEL wait routing, free-tier docs evidence |
| Provider: gemini | `docs/agents/providers/gemini.md` | gemini scans/validation: `/v1beta/models` list gate is real (403 no-key, 400 API_KEY_INVALID for bad keys), width-39 `AIza` corpus shape, `RESOURCE_EXHAUSTED`→NO_QUOTA pin |
| Provider: doubao (Volcengine Ark) | `docs/agents/providers/doubao.md` | doubao scans/validation/push: UUID-v4 corpus shape, clean `/api/v3` 401 gate, 404→NO_MODEL wait routing, push-protection supply suppression |
| Provider: mistral | `docs/agents/providers/mistral.md` | mistral scans/validation: real `/v1/models` 401 gate (no-key and bad-key alike), prefix-less 32-alnum corpus shape (env+domain anchored pattern), unverified `mistral-small-latest` probe id, 429 rate-limit vs quota split |
| Provider: exa | `docs/agents/providers/exa.md` | exa scans/validation/push, plan/quota handling or its config preset |
| Infra: egress routing | `docs/agents/egress.md` | changing `_pick_proxy`, `HARVESTER_PROXY*`, the egress pool, exit failover or compose proxy env |
| Audit: 2026-09-28 yield cull | `docs/agents/audits/2026-09-28-yield-cull.md` | deciding whether to enable/disable a provider (cull rationale) |
| Audit: 2026-09-28 lifecycle/disk | `docs/agents/audits/2026-09-28-evening.md` | changing restart/lifecycle reconciliation or disk retention bounds |
| Audit: 2026-09-30 fix sprint | `docs/agents/audits/2026-09-30-fix-sprint.md` | touching false-404 handling, lifecycle or elasticity |
| Audit: 2026-10-09 frequency | `docs/agents/audits/2026-10-09-frequency.md` | changing schedules/frequency policy or the fleet Fastly probe |

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

Production container: `harvester-web`. As of 2026-10-29 the image is built by
CI on every push to `main` (or a manual `workflow_dispatch`) and published as
`ghcr.io/highkay/harvester:latest` + `sha-<sha7>` (see
`.github/workflows/release-image.yml`). Both compose files now set
`image: ghcr.io/highkay/harvester:latest`, with the `build:` block retained as a
local fallback, so fnos no longer needs a local `docker build`. fnos git state
is often **ahead of `origin/main` by local commits** (verify with `git status`
before assuming).
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

Primary path — the image is built by CI (push to `main` / `workflow_dispatch`),
not on fnos:

```bash
ssh admin@fnos
cd /home/admin/harvester
git fetch origin && git pull origin main                   # bring in the new compose/image ref
docker pull ghcr.io/highkay/harvester:latest               # direct pull FIRST (measured 2026-10-29)
# Only if that pull fails: check the docker daemon's proxy config (the daemon,
# not the shell, needs HTTP_PROXY/HTTPS_PROXY), then fall back to the CN mirror:
#   docker pull ghcr.1ms.run/highkay/harvester:latest
#   docker tag ghcr.1ms.run/highkay/harvester:latest ghcr.io/highkay/harvester:latest
cp docker-compose.hostnet.yml docker-compose.yml           # fnos runs host networking
docker compose up -d
curl -s http://localhost:8002/health                       # expect {"status":"ok"}
```

Before pulling, review fnos-local commits (`git log origin/main..HEAD`) so the
update preserves intentional local changes, and reconcile drift (see the warning
above) before the pull. Re-applying the hostnet `cp` is idempotent and keeps
prod from reverting to bridge networking.

**Emergency-only fallback (2026-09-23 cp-overlay recipe — superseded by the CI
image flow above; use it only when GHCR is unreachable or a fix must ship
without waiting for CI)**. The fnos repo dir is NOT a bind-mount for code (only
`./data:/app/data` is), so a deployed container is `image + whatever was cp'd`.
Align + publish with:

```bash
# on fnos, in /home/admin/harvester (repo must equal the SHA you validated)
RB=rollback-$(date +%Y%m%d-%H%M%S); mkdir -p "$RB" && cp Dockerfile.web docker-compose.yml "$RB"/
git fetch origin && git log --oneline origin/main..HEAD    # must be EMPTY
git status --short                    # review uncommitted tracked edits: the reset destroys them
git reset --hard origin/main
cp "$RB/Dockerfile.web" Dockerfile.web
cp docker-compose.hostnet.yml docker-compose.yml           # or prod reverts to bridge
for d in config constant core manager provider search stage storage tools web examples; do
  docker compose cp "$d" harvester-web:/app/; done
for f in main.py web_main.py __init__.py requirements.txt; do
  docker compose cp "$f" harvester-web:/app/$f; done
docker compose restart && curl -s http://localhost:8002/health
```

- **Never cp `data/`** (it *is* the live bind mount — you would nest
  `/app/data/data` under a running scan), nor `logs/`, `tests/`, `.git/`, `.omo/`,
  the legacy in-tree `serpapi_proxy/`.
- **Copy only a committed SHA**: the workstation tree routinely carries another
  session's uncommitted WIP (e.g. 2026-09-23: `main.py`, `manager/pipeline.py`,
  `stage/definition.py`, `storage/persistence.py`), so `cp` from that directory
  would ship untested code. Validate in a clean worktree
  (`git worktree add ../x origin/main`) and copy from fnos's reset tree.
- A `restart` kills in-flight scans (reconciled as
  `failed: interrupted by service restart`) and it also drops any watcher
  processes other sessions started in the container; container env is frozen at
  creation, so a stale `HARVESTER_PROXY_*` value survives until a recreate.
- Verified state 2026-09-23: prod tree whole-cp'd at `0e3b8fc` (9/9 md5 markers,
  `/health` ok, `github_blob_to_raw` smoke), rollback copies under
  `rollback-20260923-122852/`.

### Env / keys on production

- `.env` on fnos holds `WEB_AUTH_KEY`, `ENCRYPTION_KEY`, gpt-load/tavily keys.
- `ENCRYPTION_KEY` is the AES master key — rotating it makes stored tokens
  undecryptable. Back it up.
- Self-bootstrap kill-switch: `HARVESTER_SELF_BOOTSTRAP` (default `1` = on).
  NOTE: fnos container currently does NOT pass this env var (pre-github-feature
  compose file) — the github self-bootstrap feature must be deployed to fnos
  before it takes effect there.

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

## Ops: zero-yield root-cause audit + schedule overhaul (2026-09-24)

Audited why kimi/glm/groq etc. produced 0 valid keys (evidence +
probe scripts + librarian sources: `.omo/evidence/ulw-20260924-zero-yield.md`).
Verdict: the scanning/judgement logic is NOT broken (12/12 re-probed bucket
keys were classified correctly); zero yield decomposes into four causes —
(A) pool policy (glm/glm-ai: 5.3-flash-only, fixed — see `docs/agents/providers/glm.md`),
(B) quota-dead supply (kimi ¥15-voucher keys suspended, deepseek 402, mimo-cn
401; these providers are NOT in GitHub secret scanning at all, so death =
balance exhaustion by competition, not revocation), (C) GitHub partner
auto-revocation (groq: partner + push protection + validity check → leaked
`gsk_` keys die within minutes; **groq was REMOVED from the seed list and
disabled on prod 2026-09-24**), (D) search starvation (the shared 7-token
GitHub code-search pool is drained by the morning chain — 4529 cooldown
warnings on 2026-09-24 concentrated 10:00-16:00 Beijing; kimi-ai/kimi-coding/
mimo-sg/qwen-intl starved, qwen-intl completed with 0 links).
**Stagger applied 2026-09-24: kimi-ai 18:00 / kimi-coding 19:00 / mimo-sg 20:00
/ qwen-intl 21:00 Beijing** (was 14/15/16/17) via `PUT /api/schedule/{p}`
(hot reload, no restart), seed list in `web/scheduler.py` updated to match.
Open follow-ups: the github self-bootstrap loop yields ~1 valid per 9705
materials (GitHub auto-revokes its own PATs) yet eats the most search quota —
consider cutting it to daily; add more GitHub tokens manually (user deferred).

## Hardening pass (2026-09-22, part 2): anchoring, pattern house rule, run accounting, scheduler

- **Extraction anchored: deepseek / qwen / qwen-intl / mimo-cn / mimo-sg**
  (same kimi-style fix, same day): naked task-level patterns
  (`sk-[0-9A-Za-z_-]{20,}`, `tp-…`) made every bare `sk-`/`tp-` run on a
  fetched page a candidate. **Measured real formats (prod corpus
  2026-09-22)**: deepseek `sk-` + **32 hex** (n=401), qwen `sk-` + **32 hex**
  (n=315), mimo Token Plan `tp-` + **48 alnum** (n=1). Flood measured: one
  deepseek run yielded 477 authentic keys against **2572 invalid** bare-`sk-`
  candidates (sk-ant- 186, sk-or-, other vendors); qwen ~1441 invalid/run;
  mimo ~142 invalid/run (`tp-secret-` / `tp-your-` hyphenated placeholders).
  Task patterns now fire only on env-name assignments (`DEEPSEEK_*`,
  `DASHSCOPE_*`/`QWEN_*`, `MIMO_*`/`XIAOMIMIMO_*`) with the
  `sk-ant-|proj|svcacct` lookahead, and every host-named dork
  (`api.deepseek.com`, `dashscope.aliyuncs.com`, `dashscope-intl.aliyuncs.com`,
  `xiaomimimo.com`) carries the wide Bearer/quoted/plain per-condition
  override (the query is the anchor). No upper bound on the body — the
  measured widths are documented, not enforced (serpapi lesson).
  **mimo coverage gap**: the re-flood guard captures `tp-` ONLY; mimo
  **`sk-` pay-as-you-go** keys (api.xiaomimimo.com) are NOT scanned — a
  different, unmeasured vendor surface. **Tripwire**: if one of these
  providers shows near-zero candidates reaching the check stage, widen back — but re-measure the corpus first: on kimi-coding the
  2026-09-26 sample found 0 wide-only candidates that were anything but
  placeholders (see `docs/agents/providers/kimi.md`), so "widen" is a hypothesis, not a reflex.
  (drop the anchor) and re-measure before trusting the narrowing. Pinned by
  `tests/test_deepseek_pattern.py` / `test_qwen_pattern.py` /
  `test_mimo_pattern.py` (defaults↔examples lockstep included).
- **House pattern convention (census of 129 patterns, 2026-09-22)**: ZERO
  capture groups is the norm — naked patterns let the whole match be the key
  (`re.findall` returns strings); census found 0 compile errors and 0 shipped
  multi-group patterns. Context-anchored patterns (kimi/agnes/deepseek/qwen/
  mimo lineage) use exactly ONE group to cut the key out of its env
  assignment. A blanket "group count must be 1" rule would therefore be
  WRONG — it would force every naked pattern into a capture group for no
  gain. The real hazards are (1) **multi-group patterns**: `findall` starts
  returning tuples, so extraction silently grabs fragments or chokes; (2)
  **zero-group patterns whose match INCLUDES context**: whole match =
  env-name + quotes + key, so junk reaches the candidate material and the
  check stage.
- **Failed-run accounting + cancel guard (web/db.py, web/runner.py)**: prod
  carried **54/54 failed rows with NULL `duration_seconds`** — the
  restart-reconciliation path never recorded runtime or key counts.
  `reconcile_running_runs` now computes duration per row in SQL from the
  row's own `started_at` (julianday diff — the dead process took its
  Python-side timer with it) and counts `valid_keys_found` strictly from
  THAT row's provider directory (never a foreign provider's numbers).
  Cancel guard: `run_scan` pre-registers the cancel event BEFORE the row
  insert/thread start so an early cancel is never dropped (`_execute` honours
  a pre-set event as "Run cancelled before start"), and `_update_run_sync`
  gained `only_if_running` (a terminal write from the scan thread can no
  longer clobber a `cancelled` row) + `duration_from_started_at` (SQL-computed
  duration for failure paths). Pinned by `tests/test_web_runner_lifecycle.py`.
- **Scheduler seed list 15 → 19 rows** (`web/scheduler.py::_DEFAULT_SCHEDULES`):
  added `("groq", "0 1 * * *", examples/config-groq.yaml)`,
  `("ollama", "20 3 * * *", …config-ollama.yaml)`,
  `("openrouter", "40 8 * * *", …config-openrouter.yaml)`,
  `("nvidia", "50 11 * * *", …config-nvidia.yaml)`. The `github 50 */6 * * *`
  entry was ALREADY seeded (an earlier AGENTS claim that it was missing is
  stale). Crons mirror prod's hour chain (groq 01 — the row inserted manually
  on 2026-09-03, ollama 03, openrouter 08, nvidia beside modelscope's 11)
  at minutes that collide with nothing else in the list. Deliberately NOT
  seeded: **grok** (wait-only by design — provider routes web/SSO findings to
  manual verification), **hf** (measured ~0 yield), **cerebras**
  (Cloudflare-blocked egress; disabled on prod), **opencode** (separate
  workstream, uncommitted). Seeding only fills an EMPTY `schedule_config`, so
  prod (18 rows / 17 enabled, hour chain 01-17 + disabled cerebras) is
  untouched: a direct prod DB dump (2026-09-22, 18 rows) shows prod ALREADY
  HAS the **groq** (`0 1 * * *`), **ollama** (`0 3 * * *`) and **openrouter**
  (`0 8 * * *`) rows — `docs/agents/providers/ollama.md` records their daily prod
  runs — so the only rows missing on prod are **github** and **nvidia**.
  Audit first, then run the idempotent inserts (ON CONFLICT DO NOTHING keeps
  any existing prod row — and its tuned cron — intact; the ollama/openrouter
  inserts are therefore no-ops on prod but harmless, and the fresh-install
  seed list still carries all four):

  ```sql
  SELECT provider_name, cron_expression, enabled, config_file
    FROM schedule_config ORDER BY provider_name;  -- audit
  -- the two rows actually missing on prod (2026-09-22 dump):
  INSERT INTO schedule_config (provider_name, cron_expression, enabled, config_file)
    VALUES ('github','50 */6 * * *',1,'examples/config-github.yaml')
    ON CONFLICT(provider_name) DO NOTHING;
  INSERT INTO schedule_config (provider_name, cron_expression, enabled, config_file)
    VALUES ('nvidia','50 11 * * *',1,'examples/config-nvidia.yaml')
    ON CONFLICT(provider_name) DO NOTHING;
  -- prod already has ollama (`0 3 * * *`) and openrouter (`0 8 * * *`), so
  -- these are no-ops there (DO NOTHING preserves the prod crons); kept for
  -- any DB that lacks them:
  INSERT INTO schedule_config (provider_name, cron_expression, enabled, config_file)
    VALUES ('ollama','20 3 * * *',1,'examples/config-ollama.yaml')
    ON CONFLICT(provider_name) DO NOTHING;
  INSERT INTO schedule_config (provider_name, cron_expression, enabled, config_file)
    VALUES ('openrouter','40 8 * * *',1,'examples/config-openrouter.yaml')
    ON CONFLICT(provider_name) DO NOTHING;
  ```

  **Do NOT run `scripts/optimize_schedules.py` against prod.** Unlike the
  DO-NOTHING inserts above it performs an UPSERT (`ON CONFLICT(provider_name)
  DO UPDATE SET cron_expression=excluded…, enabled=excluded…,
  config_file=excluded…`, with `enabled` inserted as the literal `1`), so it
  OVERWRITES prod's manually/UI-tuned crons and config paths and
  force-enables every default-list row (e.g. ollama `0 3` → `20 3`,
  openrouter `0 8` → `40 8`); only providers outside `_DEFAULT_SCHEDULES`
  are left untouched. The DO-NOTHING SQL above is the safe path — reconcile
  any cron differences via the UI.

  A restart (or schedule CRUD via the API) is what turns new rows into
  APScheduler jobs — `init_scheduler` rebuilds jobs from the table at startup.
- **Scheduler anti-overlap guard bug (FIXED)**: `_run_provider_job` released
  `_running` in `finally` as soon as `runner.run_scan` returned — and
  `run_scan` returns when the scan *thread starts*, not when the scan ends.
  Net effect during any live scan: `is_running()` claimed idle,
  `POST /api/schedule/{p}/run` answered **202 "triggered"**, and the runner
  then rejected the duplicate with its own 409 — which the broad except in
  the job callback swallowed. The UI reported success for a run that never
  happened (no new run_records row); cron double-firings were equally
  invisible. Fix: `SchedulerService.start_scan` holds the guard and arms a
  watcher task that polls the run's `run_records` row via the runner's public
  `get_run` every `_WATCH_POLL_SECONDS` (5 s), releasing `_running` only when
  the row leaves `running` (completed/failed/cancelled — or vanishes); a
  failed start releases the guard and propagates; `trigger_manual` AWAITS the
  start so 409 (either guard) and 404 (missing schedule row / missing config
  template, mapped from the runner's ValueError) reach the route truthfully;
  `shutdown()` cancels the watchers. Pinned by
  `tests/test_web_scheduler.py::TestGuardHeldForScanLifetime` (5 tests; the
  file is at 21).
- **Provider status-map hardening (pinned by
  `tests/test_provider_status_maps.py`, mocked responses, no network)**:
  `stage/definition.py::CheckStage` routes NO_MODEL / NO_ACCESS /
  BAD_REQUEST / is_retryable (NETWORK_ERROR, TIMEOUT, 5xx, RATE_LIMITED) →
  `wait-check-keys.txt` (recoverable) and INVALID_KEY / UNKNOWN →
  `invalid-keys.txt` (permanent discard). Any provider that fabricates
  INVALID_KEY for a recoverable state permanently burns live keys, so:
  **azure / doubao / qianfan** 404 → NO_MODEL (wrong model/deployment
  routing, not a dead key); **stabilityai / anthropic** transport failures
  (TLS EOF, timeout) → retryable → wait, never INVALID_KEY; **openrouter**
  403 → NO_ACCESS; **openai_like** 200-with-error-body guard — both shapes
  closed: a dict `error` AND a plain-STRING `error` body (native-Ollama
  shape; string arm fixed 2026-09-23) are classified by the same auth/quota/
  generic markers, so the latent hole documented in `docs/agents/providers/ollama.md` is
  fixed and test-pinned;
  **BAD_REQUEST** moved from invalid-discard into the wait bucket (trade-off
  below).
- **BAD_REQUEST→wait tripwire**: the reroute keeps recoverable 400s out of
  the permanent-discard bucket, but genuine 400-junk from providers that map
  a plain 400 to `ErrorReason.BAD_REQUEST` — **qwen** (non-Arrearage 400s),
  **tavily**, **serpapi** — now accumulates in `wait-check-keys.txt` too,
  and the wait-pool recovery recipe (≥5 s/key) will keep re-probing those
  entries every pass without ever clearing them. Watch wait-pool growth per
  provider; if it balloons with pure BAD_REQUEST entries, revisit the
  routing (or map those providers' 400s to INVALID_KEY).
- **Task ids EMBED RAW KEYS — never log them verbatim (fixed 2026-09-23)**:
  `stage/definition.py` builds the CHECK/INSPECT dedup ids as
  `check:<provider>:<key>:<address>:<endpoint>` and task dataclass reprs
  embed `Service(key='<raw>')`, while the global RedactionFilter deliberately
  misses prefix-less formats (SerpApi 64-hex has no patterns.py entry, bare
  `ms-` neither) — so every stage log site that interpolated a task repr or
  raw id (the four worker `logger.error` lines in `stage/definition.py`; the
  not-accepting / queue-full / max-retries discard lines and the requeue
  warning in `stage/base.py`) now prints a digest identity
  `provider:TaskClass:sha256(dedup-id)[:12]` via
  `BasePipelineStage._safe_task_identity` (pinned by
  `tests/test_queue_persistence.py`). The id format itself is deliberately
  UNCHANGED; a future change should hash the key INTO the id (a
  deterministic digest preserves dedup semantics) so no id ever carries key
  material.

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
- **groq**: see the 2026-09-21 correction in `docs/agents/providers/groq.md` — trio+socks5 now
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
- **Restart-safety check: query `run_records`, NEVER `ps`/`/proc` (learned
  2026-09-23, the hard way)**: scans run as THREADS inside `web_main.py`, so a
  process listing shows only `web_main.py` even with 6 scans live — and
  `docker compose restart` kills them all. Before ANY restart run
  `SELECT provider_name FROM run_records WHERE status='running';` (or check the
  UI runs page); an empty result is the only safe window. With the daily chain
  01:00-17:00 that window is roughly 18:30-01:00. The 2026-09-23 restart
  mid-chain silently discarded groq 3h21m / deepseek 2h21m / ollama 1h21m /
  kimi 21m of scanning (startup reconciliation then recorded them `failed:
  interrupted by service restart` WITH duration + own-provider valid counts —
  the new accounting made the loss measurable instead of invisible). Leftover
  work after any restart: check each killed provider's `valid-keys.txt` and
  push it manually with `PushService.push_valid_keys(<provider>, <run_id>)`
  (on 2026-09-23 ollama held 55 keys, all already pooled → nothing lost).
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
  **Re-measured 2026-09-23 with the exit A/B (one known-good + one known-dead
  key)**: exit **1080 was blocked again** (`429 "blocked due to excessive
  requests"` for BOTH keys), **1090 clean** (good→200, dead→401), **1091 half**
  (good→200, dead→429), and container-direct to `api.tavily.com` times out
  entirely. That day's live run (`415a13f9…`) showed `wait 1327→1333` against
  `valid 67`; a scan uses ONE exit (`_pick_proxy` writes a single value into
  `global.proxy` of the run's runtime YAML — check it with
  `grep -i proxy /app/data/runtime/config-tavily-<run_id>.yaml`), and this run
  drew **1091**, the half-throttled one — so its wait bucket is exit-throttle
  artifact (429 lands for good and dead keys alike), recoverable via the
  ≥5 s/key wait-pool recipe rather than being a dead pool.
  Note the running container's env still carries the STALE pin `1080,1091` even
  though both compose files now resolve `${HARVESTER_PROXY_TAVILY:-}` to empty
  (= inherit the trio), because container env is frozen at creation and a plain
  `restart` does not re-read it: dropping the pin needs a
  `docker compose up -d` recreate — which rebuilds from the IMAGE and wipes the
  whole `cp`-ed code overlay. Re-publish after any recreate with the whole tree,
  not just your own files:
  `for d in config constant core manager provider search stage storage tools web examples; do docker compose cp $d harvester-web:/app/; done`
  plus `main.py` / `web_main.py`, then re-run the md5/marker verification. (A
  working `--build` would end this drift, but fnos's registry proxy is dead.)
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

- **`persistence.batch_size: 1` in the examples was a mistake (fixed
  2026-09-22)**: `manager/pipeline.py` feeds it to `ResultBuffer`, so 1 means a
  file write + an INFO line per item — measured 64,893 `Saved 1 links items`
  lines in 6 h (70% of all INFO). The examples now use 50 (the schema default);
  `save_interval` (5 s) still bounds the flush latency, and a crash can only
  lose the in-flight buffer.
- **Expected check outcomes are not ERRORs (fixed 2026-09-22)**:
  `search/client.py::chat` now logs 400-404/429 at debug — every invalid key
  answers 401 and the glm policy deliberately drives free-tier keys into
  429/1113, which had produced 1200 of 1713 ERROR lines in 6 h. 5xx, timeouts
  and connection errors stay ERROR.

## Hardening pass (2026-09-23, Oracle audit of the scanning path)

An Oracle audit of the end-to-end scanning path found the following, all fixed the same day (committed, deployed to prod; suite 809 OK / 8 skipped):

- **Gather fetched the HTML blob page, not the file** — the day's biggest find; full story in the "Feature: gather fetches RAW file bytes" section above (85-118x bandwidth, 444 escaped quotes, 101/125 quote-anchored patterns defeated).
- **Rate-limiter denial was indistinguishable from a zero-result dork** (`search/client.py`): a denied page-1 returned an empty result, silently dropping the dork's whole pagination + refine tail at DEBUG. It now raises a retryable `ConnectionError` at WARNING, and the first-page gate distinguishes "failed" from "genuinely empty".
- **Stage retry machinery was dead**: all four workers swallowed exceptions before `_worker_loop` could requeue; search/gather workers now re-raise retryable errors (predicate reused from `RetryCore.should_retry_error`). check/inspect stay conservative because verdict routing is not idempotent.
- **401 bodies were never read** (`chat()`): judges never saw a 401 body (so glm's documented 401 sub-code map could never fire), and a stale body could travel with a fresh status. Bodies are now read for every status.
- **512-candidate cap truncated non-deterministically** (set iteration order) when metadata axes are singletons; keys are now sorted before capping.
- **openai_like empty-200**: an empty HTTP-200 body was a permanent discard (UNKNOWN→invalid); now SERVER_ERROR (retryable→wait-check). Present-but-unparseable bodies keep UNKNOWN.
- **`HarvesterApp.initialize()` was not idempotent**: every web scan initialised twice (config parsed twice, transport/shared limiter rebuilt and backoff reset, 2 extra sqlite connections, and the runner's completion listener silently discarded). It now early-returns once initialised; a failed init does not latch. Side effect: the completion listener is now live — hence the generic gpt-load push gained per-(provider, run_id) idempotency (mirroring the per-provider services, no shared base).
- **Per-stage dedup window was smaller than its queue** (100k default vs 100-500k queues → id eviction re-gathered/re-validated the same URL/key): it now derives from the queue size (`max(2×, 1000)`).
- **Periodic snapshots never started** (`start_periodic_snapshots` ran while the managers dict was lazily empty): they now start in `_on_start` (eagerly creating each provider's ResultManager; `_on_stop` also stops them for a stage-less run).
- **Zero-yield observability (the recurring "healthy-looking scan, zero yield" class)**: `run_records` gains `links_total`/`materials_total` (migration included) + per-provider gather counters (`ok`/`empty`/`error_404`/`error_other` on `ResultManager`, bridged via a WeakKeyDictionary provider→manager registry). When a run gathers >1000 links and extracts 0 materials, one ERROR line fires and `run_records.error_message` records it (status stays `completed` — the UI's four-value constraint is respected). Counter bumps sit before the retryable re-raise so every attempt is counted.
- **Deliberately left open (separate workstreams)**: per-run session transport (today one process-wide `set_proxy`/`configure_github_transport` rebinds egress for every in-flight scan — the agnes-ai 2026-09-04 mass-NETWORK_ERROR class), and link-index retention (measured 2026-09-23: `links.db` 888 MB / 1,896,671 rows with no eviction; `-wal` stays ~5 MB so no checkpoint starvation; `responses.db` was 551 MB because cached bodies were blob pages — the raw-bytes fix shrinks it ~180x). **2026-10-09 CORRECTION on responses.db**: the 543 MB file is NOT blob pages and did NOT shrink — it is the 1000-row / TTL-60 s hot cache of `api.github.com/search/code` JSON (each page response is legitimately ~0.5 MB with text_matches fragments). SQLite reuses freed pages in place, so the file sits at its ~543 MB high-water mark (stable 551→543 MB over 16 days, bounded ≈600 MB by the row cap; ~71 MB free pages reclaimable only via VACUUM). links.db itself: write-gated since 09-28 and verified still 36 KB on 10-09.

## Feature: gather fetches RAW file bytes (fixed 2026-09-23; was: HTML blob pages)

- **The defect (found by an Oracle audit, then verified on prod)**: every gather
  fetch used the search result's `html_url` verbatim —
  `https://github.com/<o>/<r>/blob/<ref>/<path>` — i.e. GitHub's React HTML
  blob PAGE, not the file. The page carries the source JSON-escaped inside its
  `rawLines` payload, so every `"` in the file arrives as `\"`, and the page is
  far larger than the file. Measured per link through the prod socks exit:
  blob **268 952 B** vs raw **3 159 B** (85x); 1 115 687 vs 72 962; 508 794 vs
  20 371; 262 564 vs 2 217 (**118x**). The fetched blob page contained **444
  escaped quotes**; `github.com` direct connections time out from the container
  (the page is only reachable through a socks exit).
- **The damage**: the quote-anchored extraction patterns this repo converged on
  (101/125 shipped `key_pattern`s carry a `["']`-style anchor) cannot match a
  quoted assignment in that text — the repo's own deepseek task pattern matched
  the RAW body and returned `[]` on the blob text; quoted forms
  (`KEY="sk-…"`, `key: "sk-…"`) are the dominant shape in .env/python/yaml/json
  files. Every blob fetch also burned 85-118x the bandwidth, a direct cause of
  the measured multi-hour gather phases (a `github` run took 5.7 h) on the
  4-core NAS.
- **The fix**: `stage/definition.py::github_blob_to_raw()` maps
  `https://github.com/<o>/<r>/blob/<ref>/<path>` (± `#L…` fragment) to
  `https://raw.githubusercontent.com/<o>/<r>/<ref>/<unquoted path>` and leaves
  every other URL (hf `resolve`, issue/commit pages, non-github, query strings,
  malformed input) byte-identical. It runs in `_acquisition_worker` right
  before `collect(...)`; `links.txt` and the dedup id keep the ORIGINAL blob
  URL. `collect()` gained an optional `headers=None` passthrough so the gather
  call site sends a real `User-Agent` (`get_user_agent()`) — gather had been
  identifying as `python-requests/x.y.z` anonymously from one exit IP.
  `raw.githubusercontent.com` is reachable DIRECTLY from the container (200 in
  0.4 s) and via both socks exits.
- **Verification recipe** (reuse for ANY pattern-anchoring change): fetch the
  same link both ways and run the shipped pattern against both texts — raw must
  match; if only the blob form matched, the pattern is anchored to escaped
  text. Pinned by `tests/test_gather_raw_url.py` (15 tests: helper semantics,
  the wiring, and two extraction regressions on quoted assignments).

## Ops: 2026-10-18 yield/frequency audit → egress pins + watchdog salvage + weekly floors

Deployed `eb23241` (five atomic commits, full suite 1324 OK / 8 skipped).
Evidence: `.omo/evidence/eval-20261018/`.

- **The audit trigger**: brave/exa/nvidia catch-up runs sat 32h+ in `running`
  (check-drain bound; box idle at load 0.89 with a 36.8 MB shared check
  queue), heading for the 36h watch deadline. Root causes found and fixed:
- **Per-host proxy pins (the big one)** — `search/client.py`
  `pin_host_proxy(host, proxy)` + `_pinned_session_for()` routing in
  `request()`: a pin binds a host to a dedicated session that later
  `set_proxy()` rebinds cannot reroute. Registered by
  `web/runner.py::_pin_dedicated_provider_hosts` at scan start (in
  `_generate_temp_yaml`) ONLY when a dedicated `HARVESTER_PROXY_<PROVIDER>`
  override exists (hosts read from each task's `api.base_url`). Measured root
  cause: brave needs socks5h (plain socks5 relays to the DNS-poisoned local
  answer → ConnectTimeout; direct times out), its 13:51 run pinned socks5h,
  the 13:00 glm-ai scan's plain-socks5 rebind stole the process session, and
  every brave check died NETWORK_ERROR → wait-check (2,326 wait vs 13 invalid
  vs 0 valid; the 13 invalid all date from the run's own first hour).
  Global-rotation providers are deliberately NOT pinned (they must follow the
  shared session's failover); pinned traffic feeds neither exit-health streak;
  `use_proxy: false` and `_DIRECT_HOSTS` still win over pins; no failover
  inside a pin (its plain-socks5 siblings are exactly what the pin avoids — a
  dead pinned exit surfaces as retryable wait-check until the next run
  re-pins). Live proof 2026-10-18 23:33:30: the brave catch-up run logged
  `Pinned host api.search.brave.com to proxy socks5h://…1080`; exa/nvidia
  catch-ups (no override) logged no pin. Supersedes the "scheme-consistent
  candidate list" note in `docs/agents/providers/brave.md` — candidates alone could never
  fix the rebind direction.
- **Watch-deadline salvage push** — `web/scheduler.py::_watch_run`: after the
  36h deadline flip, when `valid_keys_found > 0` the watcher now dispatches
  `_dispatch_provider_pushes` (the same idempotent path the completion hook
  uses; a race with a late terminal write is absorbed by per-run dedup).
  Before this, a deadline-flipped run's keys were stranded until the next
  restart's startup recovery — and each provider's next clean-start run
  (`auto_restore: false`) resets `valid-keys.txt` first.
- **exa dork4 removed** — `"x-api-key" "exa"` was 88.5% of measured supply
  and 99.95% noise on prod (129,416 materials → 25 valid in the stuck run):
  "exa" matches example/examination pages, so the anchor is broken at the
  QUERY level and the wide override harvested every hex-ish string; at ≈26
  verdicts/min the check stage can never drain it. Removed from
  `examples/config-exa.yaml` + `config/defaults.py`; remaining supply ≈95k ≥
  10k keeps the daily tier. Re-add only with a corpus-measured pattern.
- **Frequency round 2** (house rule stands: weekly floor, never full
  disable): applied on prod via PUT /api/schedule (hot, verified
  next_run_time) + seed list synced. glm `0 12 * * mon,wed,fri` (9 net-new /
  5,933 pushed in 12d), glm-ai `0 13 * * wed` (1 net-new; 1.28M links/added
  — the most saturated), ollama `0 3 * * mon`, openrouter `0 8 * * thu`,
  agnes-ai `0 10 * * fri` (2-4 net-new/12d each). Unchanged daily:
  tavily 09, nvidia 11:50, glm 12 (on its days), brave 15, exa 16. APScheduler
  dow footgun from 2026-10-09 still applies: NAME the weekdays.
- **Salvage executed before the deploy restart**: exa run `ba2cdaad` 25 keys
  → +16 (id 671), nvidia run `2dd5d87a` 131 keys → +1 (id 672); brave's run
  had 0 valid (wait-flooded) so nothing to salvage. The restart then
  reconciled the trio and armed catch-ups (brave +60s / exa +120s / nvidia
  +180s) — the exa catch-up is the first run on the dork4-less config.
- **Baseline table for the frequency round** (12d, links→valid→net-new):
  nvidia 2.0万 links/added (keep daily), tavily 2.6万 (keep), serpapi ~7.4万
  real (213 of 227 added were the hikari migration backfill), agnes-ai 4.4万,
  ollama 5.2万, modelscope 14.7万, openrouter 14.6万, glm 21万, glm-ai
  128万. Yield trends 10-07→10-18: tavily 1816→1127, glm 519→343, glm-ai
  61→28, nvidia 1028→904, serpapi flat ~600.

## Ops: 2026-10-19 six-agent audit wave → extraction tightening + schedule hours + wait salvage

Six parallel evidence agents (A yield / B logs / C schedule / D pools / E search
/ F validation) audited prod read-only; reports: `.omo/evidence/eval-20261019/agent-{a..f}-*.md`.
Deployed `b5c20b7` + `e5a71a7` (config cp only, no restart; full suite **1340 OK
/ 8 skipped**). The wave's findings and what was done with them:

- **Validation judges are trustworthy (F)**: 233 invalid-bucket re-probes, 0.0%
  misclassification. The wait buckets, though, are full of recoverable keys —
  nvidia 91/94, glm 173/183, glm-ai 25/31 re-validated as VALID — because they
  park keys that hit transient 429/1305 congestion and nothing re-checks them.
  Salvage ran 2026-10-19 (script `.omo/evidence/eval-20261019/wait_recovery_20261019.py`,
  container /tmp/waitcopy/): nvidia +1 / glm +1 / glm-ai +0 net-new (the pools
  already held most of them — the hygiene value is the fresh alive signal).
- **Extraction tightened to measured key shapes (E's P6)** — whole-corpus
  measured, 100% of current valid keys retained in every provider: brave wide
  override now captures only `BSA`-prefixed (case-SENSITIVE prefix via scoped
  `(?-i:...)`) — cuts materials -99.6%; exa UUID shape -90.5%; ollama
  `<32hex>.<16+>` -84.7%; modelscope `ms-` floor 36 -51.3%; openrouter `sk-or-v1-`
  floor 64 -30.9%; tavily floor 32 -26.3%; nvidia `nvapi-` floor 64 -24.7%;
  agnes-ai `sk-` floor 48 + `or|kimi` vendor exclusions -6.6%. Pinned by
  `tests/test_pattern_tightening_20261019.py` (defaults↔examples lockstep
  included) + updated brave/exa/modelscope pattern tests. Tripwire: if a run's
  candidate volume collapses to near-zero, re-widen that provider's floor.
- **glm/glm-ai body charset widened** `[A-Za-z0-9]` → `[A-Za-z0-9_-]`
  (extraction + `tools/patterns.py` redaction — the redaction half only takes
  effect on the next restart): 52% of measured valid keys on the SHARED
  `<32hex>.<body>` shape (ollama corpus) carry `_`/`-`, so glm-format keys with
  such bodies were neither extractable nor redactable. glm's own corpus is
  all-alnum today — re-measure after one run; if candidate count collapses,
  revert.
- **Dork composition (E's P2/P4/P5)**: `size:` triplets added for the capped
  rich classes (tavily env+json, nvidia env+json, serpapi env, ollama env) —
  these classes sit at the 1,000-result window and their `language:` refine
  partitions are 27/27 zero-result (extension:-qualified dorks cannot be
  language-partitioned); openrouter gained its first class-qualified dorks
  (env/yaml); brave's zero-result `extension:env` dork swapped to
  `filename:.env` + two `BRAVE_SEARCH_API_KEY` class dorks; opencode's three
  broad domain dorks removed (0 page-1 keys in 3 runs, 0 valid ever).
- **Schedule hours** (C's collision-minimal proposal, applied hot via PUT +
  seed list synced): glm `0 14 * * mon,wed,fri`, glm-ai `0 16 * * wed`, brave
  `0 20 * * *`, exa `0 22 * * *`, opencode `0 18 * * sat`, agnes-ai
  `30 17 * * fri`, openrouter `30 7 * * thu`. Rationale measured: search-phase
  overlap is the only real quota cost (cooldown events concentrate in the
  2-3-way overlap windows; solo searches produce zero at any hour); the
  10:00-16:00 storm note is STALE — the real burst is tavily's own 09:00
  search phase (~29 req/min against the per-token ceiling, 09:00-11:30).
- **openrouter gpt-load group 9** had `validation_endpoint: ""` → gpt-load
  validated against OpenRouter's PUBLIC `/v1/models` (the documented
  false-positive trap, live: 951 cumulative failures, 0 marked invalid).
  Fixed to `/v1/key` via `PUT /api/groups/9` (hot, verified).
- **E's deep findings (next rounds, NOT done)**: (P1) `max_links_per_run`
  counts UN-deduplicated discoveries (2.5-4.2× dup factor) so the 120k cap
  binds at ~40k unique links and silently drops 7-19% of pagination —
  `links_total` overstates corpora by the same factor; (P3) the refine axis
  should partition by `size:` FIRST (0% zero-result vs language's 37.4%, and
  100% for extension:-qualified dorks) and skip refining partitions that stay
  >1,000; (P7) per-repo caps (one benchmark repo = 1,587 links across 4
  providers); (P8) 4 malformed link lines (spaces in paths split them →
  guaranteed false 404s, same family as the 09-30 fix).
- **D's pool-side findings (out of harvester scope — for the hikari repo /
  gpt-load config)**: hikari's forward-proxy is down to 1 of 3 exit nodes
  (brave 502s through the pool; the 11 harvested keys are fine); 530
  quota-exhausted tavily keys have no `quota_renews_at` and can never
  reactivate (~530k credits parked); gpt-load tracks failure_count but never
  evicts. **Watch item**: brave/exa catch-up runs still in flight at deploy
  time carried the OLD config; their pushes land on completion (exa already
  did: +14). The `HARVESTER_MAX_DEFERRALS` on prod is 6, not the documented 8.

- **Pipeline code fixes from the same audit (deployed `524e316`, full suite
  1353 OK / 8 skipped)**: (P1) the `max_links_per_run` cap now counts UNIQUE
  links per run (a seen-set in SearchStage) — it used to charge raw
  discoveries (2.5-4.2× duplicate factor), so the 120k cap bound at ~40k
  unique and silently dropped 7-19% of pagination; (P7) per-repo fetch cap
  (25 links/repo/run, `_github_repo_key`) — benchmark/dataset repos flooded
  every provider's corpus (one repo = 1,587 links across 4 providers);
  capped links stay in links.txt but are never fetched; (P3) refine axis
  routing: file-class-qualified dorks (`extension:`/`filename:`/`path:`)
  skip the `language:` axis (27/27 such partitions returned 0 results — 270
  wasted requests/day measured) and go straight to `size:`; (P8) the gather
  worker skips URLs carrying literal control characters (\n/\r/\t) — 4 in
  1.09M corpus lines were `<blob-url>\n<path-fragment>` strings whose
  urlsplit-based encoding silently concatenated the parts into a guaranteed
  404 (legitimate space-carrying paths like `Chopping _tracker /main.go` are
  NOT affected — a space is not a control character). Tests:
  `tests/test_search_link_cap.py` (dedup + repo cap), `test_gather_raw_url.py`
  (guard + legit-space case), `test_refine_axis_routing.py` (new).
- **hikari forward-proxy repair (2026-10-19)**: brave served 502 through the
  pool because 2 of 3 exit nodes were marked unavailable (`1080`
  handshake_timeout — transient, and `7890` the dead mihomo). One
  `POST /api/settings/forward-proxy/revalidate` re-probed both back to
  available and brave went 200 via the pool (32s). If brave 502s again, check
  `/api/stats/forward-proxy` first — the repair is one POST away.
- **Restart-salvage cycle now fully automatic**: the 2026-10-19 deploy killed
  3 live runs; startup reconciliation + `recover_unpushed_runs` re-pushed
  brave's 36 valid keys (+26 net-new to hikari) with zero manual steps.

- Run: `python -m unittest discover -s tests`. **Measured baseline 2026-10-19
  (workstation, at `524e316`): 1353 OK / 8 skipped** — includes the P1/P3/P7/P8
  pipeline-fix tests. **Measured baseline 2026-10-19
  (workstation, at `e5a71a7`): 1340 OK / 8 skipped** — includes the
  tightening/lockstep tests from the audit wave. **Measured baseline
  2026-10-18 (workstation, at `eb23241`): 1324 OK / 8 skipped** — includes the
  15 tests from the egress-pin/watchdog/frequency wave. **Measured baseline
  2026-09-28 (workstation, at `638fe74`): 932 OK / 8 skipped** — includes the
  restart-lifecycle + disk-bounds tests added that day. The 2026-09-24 baseline was
  846 OK / 8 skipped (dirty worktree on `main`). The 2026-09-23 baseline was
  726 OK / 8 skipped in a clean checkout of `0e3b8fc` (the "572 OK as of
  2026-09-22" note below was written before the same-day hardening landed; the
  old "490 tests" and "35 failures in test_web_ui / test_web_push_logs"
  baselines are stale).
- **Test-env gotchas (learned 2026-09-30, workstation)**: (0) spawning the
  venv python.exe from a subprocess with inherited stdin can HANG — pass
  `stdin=subprocess.DEVNULL` (cost a cancelled 300s test cell on 2026-10-18).
  (1) the web API tests
  (`test_web_middleware` / `test_web_auth` / `test_web_ui` /
  `test_web_push_logs`) fail with "starlette.testclient requires the httpx2
  package" if the env lacks it — `httpx2` is a TEST-only dep (starlette
  TestClient) NOT in `requirements.txt`; a fresh venv must
  `pip install -r requirements.txt httpx2`. (2) The workstation Python was
  switched 3.13 → 3.12 (uv-managed) mid-day; the F7 `TestWatchRunDeadline`
  tests went red under 3.12 because they relied on sub-50ms `asyncio.sleep`
  precision — under 3.12+Windows a tiny sleep returns ~0 ms when a concurrent
  task also sleeps (measured: 10× sleep(0.01) = 0.0 ms with a concurrent
  sleeper vs 156 ms alone). Fixed by making the shared `_released_within`
  helper wall-clock-bounded instead of attempt-counted. **Lesson: never write
  a deadline test that races real millisecond sleeps against a real
  millisecond deadline — control time or bound by wall clock.** Baseline at
  `65014bb` (py3.12 venv): **1028 OK / 8 skipped**.

- **Env-pollution trap (cost a false 35-red suite on 2026-09-23)**: the
  workstation `.env` carries the *container* paths
  (`HARVESTER_DB_PATH=/app/data/harvester.db`, `HARVESTER_WORKSPACE=/app/data`).
  `web/config.py::WebSettings.db_path` reads only `HARVESTER_DB_PATH` (while
  `web/db.py::resolve_db_path` also accepts the legacy `HARVESTER_DB` the
  harnesses set), so a polluted machine makes the app under test open an empty
  DB → `no such table: github_tokens` → every DB-reading page 500s in
  `test_web_ui` + `test_web_push_logs` (35 failures that look like a web
  regression but are not). Fix landed in `fe36dae`: both harnesses now set the
  canonical `HARVESTER_DB_PATH` too, verified green **with** the polluted vars
  set. Ad-hoc runs can also use
  `env -u HARVESTER_DB_PATH -u HARVESTER_WORKSPACE python -m unittest …`.
  (Two resolvers disagreeing is the underlying defect —
  `resolve_db_path()` is the single source of truth to delegate to.)
- **`examples/config-nvidia.yaml` now VALIDATES (2026-09-23)**: it used to be
  collection-only (`check/inspect: false`) — so the scheduled 11:50 run spent
  dorks daily and produced nothing pushable, while it is the ONLY producer for
  fnos group 11 (891 keys, `meta/llama-3.2-11b-vision-instruct`). Egress was
  measured before flipping: a never-registered `nvapi-` key answers **403
  `{"status":403,"title":"Forbidden","detail":"Authorization failed"}`** (a
  key-level rejection — that is NVIDIA's own body shape) while pooled keys
  answer **200 in 2–6 s** through exits 1090/1091 (container-direct times out;
  1080 gave the same 403, so the exits are fine for this host). The 403 branch
  of `provider/openai_like.py` now maps `authorization\s*failed` → INVALID_KEY
  (geo/Cloudflare blocks still → NO_ACCESS → wait-check), pinned by
  `tests/test_provider_status_maps.py`. Expect long runs: validation is the slow
  part of a wide-corpus profile.
- One config file = one task. Do NOT bundle regional tasks into one config
  (2026-09-21: `glm`/`kimi`/`mimo`/`qwen` were split into per-task files). A
  bundled run aggregates 2-3 tasks into one `run_records.valid_keys_found` and
  mixes statistics; pushes are per task anyway. `web/scheduler.py` now seeds
  one schedule row per task (**20 rows** since the brave seed on 2026-10-15;
  19 from 2026-09-22 until then — groq out / opencode in on 2026-09-28,
  `brave 0 15 * * *` in on 2026-10-15: high-churn */4 + */6
  layers, daily regional hours, and the groq/ollama/openrouter/nvidia
  additions — see the 2026-09-22 hardening section for the prod gap this
  leaves behind).
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