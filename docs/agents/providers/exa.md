# exa provider - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Feature: exa provider (scan + validate + push)

Landed 2026-10-16 as four atomic commits on `main`: `54f0d13` provider →
`9bc9d06` config/preset → `7badb84` push + runner hook → `8a8c725` schedule
seed + compose/env passthrough. Plan `.omo/plans/exa-search-provider.md`;
evidence `.omo/evidence/exa/` (`t1-supply.md`, `t2-egress.md`,
`t2-probe-raw.txt`, `t3-provider.md`, `t4-config.md`, `t5-push.md`,
`t7-integrate.md`, `t7-full-suite.log`, `t8-deploy.md`,
`t8-phaseb-deploy.md`). Lineage: a deliberate pure-rename mirror of the brave
wave (`provider/brave.py` → `provider/exa.py`, `web/brave_push.py` →
`web/exa_push.py`) with THREE load-bearing deviations, each flagged below.

- `provider/exa.py` — `ExaProvider` (registered as `"exa"`; import + `__all__`
  in `provider/__init__.py`, +2 lines). Base `https://api.exa.ai`, auth is the
  `x-api-key` header (no Bearer scheme, no query param), probe path `/contents`
  (used for BOTH `completion_path` and `model_path`), `default_model
  "exa-search"` (must be non-empty — `provider/base.py:105-108` raises
  otherwise).
- **Validation is a SINGLE-STAGE ZERO-COST (`零成本`) probe**: `POST
  {base}/contents` with the body EXACTLY the empty JSON object `{}` (sent as
  `json=`, no `params`, no query string, and crucially no `ids`/`urls` entry —
  any of those would turn the probe into a BILLABLE contents lookup charged to
  the candidate's own prepaid credit). Mirrors hikari's `probe_exa_contents`
  (`api_keys_and_logs.rs:420-473`, `.json(&json!({}))`), so a wide extraction
  corpus costs nothing to validate.
  - **Measured live 2026-10-16** (`t2-egress.md` + `t2-probe-raw.txt`): 7 egress
    paths × 2 runs = 14 probes from inside the fnos `harvester-web` container
    with a bogus key; EVERY one answered an exact **401** "Invalid API key…"
    with a verified TLS chain, 1.0-2.9 s. **GATE_PASS** — the probe separates an
    invalid key from a valid-but-misparsed request.
  - The success branch (**422 for a REAL key**) is still UNMEASURED: a bogus key
    can only reach 401. That row rests on hikari's live-probed
    `probe_exa_contents` plus its pinned server test
    (`provider_admin_api_keys.rs:104-126`, asserted at `:536-544`). T9's first
    valid key settles it, together with the double-probe freeness check.
- **Judge table** (`_judge_probe`) is **STATUS-driven** — the flat `tag` is only
  a tie-breaker for statuses the table does not pin, which is what hikari's
  `classify_exa_failure` (`provider_failure_classification.rs:146-166`) keys on.
  Consequence: an unparseable, tagless or garbage body on a pinned 4xx still
  yields the pinned verdict.
  - **401 → INVALID_KEY** (any body flavour: flat tag, no tag, garbage, empty,
    HTML) → invalid bucket. 401 is exa's real auth rejection (contrast brave,
    whose is 422).
  - **400 / 422 → success** ("unbilled validation error") → valid. 422 is the
    CANONICAL answer for a valid key — body `{"error":"body must contain either
    ids or urls"}` — i.e. auth proven, nothing billed. **No tag override on
    pinned rows**: a 400/422 that carried `INVALID_API_KEY` is STILL success
    (`test_422_with_an_auth_tag_is_still_valid`); hikari's pool self-heals
    anyway (a 401 on the first leased call quarantines the key,
    `proxy_provider_facade_exa.rs:74-105`).
  - **402 with ANY tag → NO_QUOTA** (`NO_MORE_CREDITS`,
    `API_KEY_BUDGET_EXCEEDED`, `TEAM_BUDGET_EXCEEDED`, unknown, none) →
    `no-quota-keys.txt`, **never pushed**. exa credit is PREPAID and hikari's
    `RecoveryPolicy::SuccessOnly` means no calendar refill — a spent key is
    permanently dead, so 402 is a KEY verdict, not a wait verdict (deliberate
    deviation from hikari, which needs a budget tag to call it Exhausted).
  - **429 → RATE_LIMITED** (retryable) → wait-check. **403 → NO_ACCESS** →
    wait-check, whether the body is a JSON envelope, a Cloudflare/HTML
    interstitial or empty: an egress/access problem must not burn the candidate.
  - **2xx → success** (defensive — an empty body cannot produce a billable 2xx;
    implemented as pinned, with no brave-style JSON guard); **5xx →
    SERVER_ERROR**; timeout/connection (`code <= 0` sentinel, body `"timeout"`)
    → TIMEOUT / NETWORK_ERROR — all retryable → wait-check. Unpinned 4xx/3xx
    with a recognised tag → that tag's reason (`_TAG_REASONS`), otherwise
    NO_ACCESS, **NOT UNKNOWN**: UNKNOWN routes to the invalid bucket
    (`stage/definition.py:781-786`), so a systematic path/egress error would
    permanently discard live keys. Empty/blank token → INVALID_KEY with **zero
    requests sent**.
- **TRAP — the envelope reader is NOT brave's** (caught by the architect before
  any code was written): exa's error body is
  `{"requestId":…,"error":"Invalid API key","tag":"INVALID_API_KEY"}` — a FLAT
  top-level `tag`, with `error` a human STRING. `_error_tag()` reads that flat
  field; brave's nested `error.code` reader returns `""` for EVERY exa answer,
  so a naive copy of `provider/brave.py` would have misfiled every invalid key
  into wait-check instead of invalid (pinned by a test asserting brave's reader
  yields `""` on the exa shape). Also inherited from brave: `_error_body()`
  reads `error.response.text` in full because
  `search/client.py::http_error_message` truncates at 300 chars, and the
  **except-HTTPError branch is KEPT** — `search/client.py:634` calls
  `raise_for_status()`, so the EXPECTED 4xx answers arrive as exceptions, not as
  responses.
- **`inspect()` is minimal on purpose**: it reuses the SAME probe (no extra
  request) and emits only `probe.status_code` and, when present, `probe.tag`.
  exa sends **no `X-RateLimit-*` quota headers at all** (hikari marks it
  `supports_quota_sync=false`), so brave's positional month-window parser
  (`_month_window` / `_positional_int` / `_quota_items`) was deliberately NOT
  copied — `_probe()` never even reads response headers, and a test supplies
  brave-style quota headers to pin that no `quota.*` item can ever appear. No
  token, no `error` prose, no `requestId` and no raw body reach an audit item.
- **Egress: NO pin needed** (`t2-egress.md`, measured from the fnos container
  2026-10-16): `api.exa.ai` is reachable on **every** path — container-direct
  AND all three `socks5://192.168.1.18:1080/1090/1091` AND all three `socks5h://`
  siblings — identical 401, TLS verified, no timeout, no hostname mismatch. This
  is the OPPOSITE of brave (direct dead, plain `socks5` DNS-poisoned, only
  `socks5h` works), so **`HARVESTER_PROXY_EXA` stays an EMPTY-default
  passthrough** in both compose files (`8a8c725`) = inherit the global
  `HARVESTER_PROXY`. Verified deployed: container env `HARVESTER_PROXY_EXA
  present=True len=0`, runtime YAML `proxy: socks5://192.168.1.18:1090` —
  designed behaviour, not a fallback. If exa is ever firewalled, all six SOCKS
  variants are pre-measured working fallbacks; prefer `socks5h://`.
- **Extraction** (`config/defaults.py` exa preset + `examples/config-exa.yaml`,
  defaults↔example lockstep-pinned): task level is env-anchored only —
  `(?i)(?:exa[_-]?(?:api[_-]?)?key)["'\]]{0,2}\s*[:=]\s*["']?([A-Za-z0-9\-]{16,})["']?`,
  ONE capture group, **no width ceiling and no prefix assumption**. Five dorks:
  `"EXA_API_KEY"`, `"EXA_KEY"`, `"api.exa.ai"`, `"x-api-key" "exa"`,
  `"exa" "api key" extension:env`; dorks 3 and 4 carry the per-condition WIDE
  override `(?i)(?:x-api-key["'\s:]*|Bearer\s+|["']\s*)([A-Za-z0-9\-]{20,})`.
  **No negative lookahead is needed here** — brave's self-match defect cannot
  recur because `x-api-key` is 9 chars, below the 20-char capture floor — and
  that property is pinned (`test_header_name_below_wide_capture_floor`,
  `test_header_line_self_match_rejected`).
  - **Supply measured 2026-10-16** (`t1-supply.md`; GitHub code search
    `total_count`, `per_page=1`, 5 requests at ~8 req/min): dork1 70,528 +
    dork2 5,320 + dork3 14,528 + dork4 729,088 + dork5 4,416 = **823,880** →
    the **daily** tier of the frequency ladder. Dork 4 alone is 88.5% of the sum
    and heavily noisy (`x-api-key` matches generic header literals; `exa`
    matches examinations/examples/…); dropping it still leaves 94,792 ≥ 10k, so
    the daily tier is robust. The price of that reach is a low signal-to-noise
    ratio in the check stage.
  - **Key format UNMEASURED (n=0)** — UUID-ish by dashboard convention
    (`dashboard.exa.ai/api-keys`), but NO width and NO prefix assumption went
    into the pattern (any 16+ `[A-Za-z0-9\-]` body). **Tripwire: measure the
    first real corpus before tightening anything** (the serpapi fixed-32 and
    brave n=1 `BSA` lessons); the push lane has **no key-shape pre-filter** for
    the same reason.
  - Redaction: still **NO `tools/patterns.py` entry** (a bare UUID/hex pattern
    would blitz log redaction). Neither the provider nor the push service writes
    key material to disk or logs.
- **Push**: `web/exa_push.py` `ExaPushService` (+ module singleton
  `get_exa_push_service()`), a pure-rename mirror of `web/brave_push.py` (428
  lines each) — hikari's `POST /api/keys/batch` is Tavily-only, so exa cannot
  use it. Env-gated by `EXA_PROXY_BASE_URL` / `EXA_PROXY_AUTH_KEY`: base empty =
  silent no-op (0 HTTP calls, no `push_logs` row — proven live by
  `qa_noop_check.py`: "HTTP attempts observed: 0"); the auth key is **optional**
  (Bearer sent only when non-empty) because prod hikari runs
  `DEV_OPEN_ADMIN=true`. Per key: `POST {base}/api/keys`, body exactly
  `{"api_key": k, "provider": "exa"}`, timeout 30 s, ×3 retry with 1/3/9 s
  backoff on 429/5xx, fail-fast on 401/403. **hikari answers 201 for new,
  existing AND undeleted keys**, so net-new = the provider-scoped
  `GET {base}/api/keys?provider=exa&per_page=1` `total` delta read before/after
  the loop, clamped to `[0, accepted]`; if either total read fails → `added =
  accepted` plus `"pool count unavailable; added counted from accepted
  responses"` in `push_logs.error_message` (never silent). **409
  `provider_conflict`** (key already lives under another provider) = **ignored,
  not failed**. Exactly one `push_logs` row per run (`gpt_load_config_id=0`,
  `group_id=0`), per-run idempotent (`_seen_run_ids` + lock, checked before any
  I/O), `push_valid_keys` NEVER raises.
- Hook: `web/runner.py::_dispatch_provider_pushes` fires the exa push iff
  `provider_name == "exa"` (daemon thread, ImportError-safe, appended after the
  brave block; log lines at `runner.py:860` / `:864`). The generic gpt-load push
  self-skips because there is no `provider_group_mapping` row for exa —
  deliberately not added. **Coupling**: the scan task name must stay `== "exa"`
  (the gate is a string compare and the source file is
  `providers/exa/valid-keys.txt`).
- Schedule: `("exa", "0 16 * * *", "examples/config-exa.yaml")` in
  `web/scheduler.py::_DEFAULT_SCHEDULES` — **daily 16:00 local**, one hour after
  brave's 15:00 and clear of the morning chain, with **no day-of-week field on
  purpose** (APScheduler numeric-dow footgun). Seeds fill an EMPTY
  `schedule_config` only, so prod got a one-off `INSERT …
  ON CONFLICT(provider_name) DO NOTHING` → **row id 93** (`0 16 * * *`, enabled,
  `examples/config-exa.yaml`, created 2026-10-16 15:03:31 UTC = 23:03 CST);
  prod table 22 → 23 rows, 12 enabled. Hot-registered at the Phase B restart:
  `Scheduled exa with cron '0 16 * * *'`, `Scheduler started with 12 job(s)`,
  and `GET /api/schedule/exa` → `next_run_time 2026-10-17 16:00:00+08:00`. The
  `config/defaults.py` preset ships `enabled: False`;
  `examples/config-exa.yaml` is `enabled: true`. Rate limit 0.2 req/s + burst 2
  adaptive (hardened-provider pace, inherited-from-lineage rather than measured
  — the lockstep test surfaces any drift). `tests/test_web_scheduler.py` seed
  pins renumbered 18/19 → **21** rows.
- **Deploy = TWO PHASES** (`t8-deploy.md`, `t8-phaseb-deploy.md`):
  - **Phase A, 2026-10-16 23:03 CST — non-disruptive only.** backup → `git
    fetch` → `LOCAL_ONLY_COUNT=0` gate → `git reset --hard origin/main` →
    restore the fnos-local `Dockerfile.web` + `cp docker-compose.hostnet.yml
    docker-compose.yml` → **6/6 host-leg md5 MATCH** → `.env` gains
    `EXA_PROXY_BASE_URL` / `EXA_PROXY_AUTH_KEY` (values copied programmatically
    from the existing hikari pair, never echoed — only key NAMES, booleans and
    lengths printed; 22 keys afterwards, no duplicates) → `schedule_config` row
    93. The safe-window guard then returned **`WINDOW_BUSY`** (brave `ecb6e000` +
    nvidia `a7e09287` live), so Phase B was NOT executed and the container was
    proven untouched (`Up 21 hours`, both `EXA_PROXY_*` still ABSENT — container
    env is frozen at creation).
  - **Phase B, 2026-10-17 13:50 CST — authorized recreate with a 4-run
    sacrifice.** `docker compose up -d` (RECREATE, for env) → whole-tree cp
    overlay → `restart`, 30 s end-to-end → `/health` ok → **6/6 md5
    host↔container MATCH** → `exa` present in the registry's 29 provider types,
    `web.exa_push._PROVIDER_NAME=exa`, `RUNNER_HAS_EXA_HOOK=True`. The restart
    killed 4 running scans (brave `ecb6e000`, nvidia `a7e09287`, glm `f1b4ecb4`,
    glm-ai `8dc3f035`), reconciled as `failed`; a collateral query found no
    other casualty. **Salvage: 802 keys accounted for, 9 net-new pooled, 0
    lost** — the app's OWN completion/push hooks had already fired during
    shutdown-reconciliation (`push_logs` rows 659-662, stamped the same second as
    `Reconciled 3 interrupted run(s) as failed`: added 9 / ignored 793), and the
    manual re-push from rotation-backup snapshots (rows 663-666) came back
    **added=0 / ignored=ALL 802**, which IS the completeness proof. Lesson: the
    restart's catch-up runs ROTATE (back up + clear) each provider's result
    files, so brave's and nvidia's live `valid-keys.txt` were already gone by
    13:54 and the casualties had to be read out of
    `backup-20261017-1351xx/` — snapshot first, push second, and stage the
    snapshot in the container's own `/tmp`, never the `./data` bind mount.
- **First run** (measured 2026-10-17 ~14:10 CST): the scheduler's catch-up armed
  it 120 s after the Phase B restart — `Scheduled catch-up run for exa: missed
  cron fire at 2026-10-16T08:00:00+00:00, firing in 120s` — and run
  **`ba2cdaad-0820-42cc-b30c-071dddee99ce`** started 05:52:28 UTC (13:52:28
  CST) with NO manual `POST /api/schedule/exa/run`. The missed fire is
  yesterday's `0 16 * * *` in LOCAL time (16:00 CST = 08:00 UTC); Phase A's
  "16:00 UTC" reading of that cron was off by the UTC+8 offset. Sixteen minutes
  in it was **still RUNNING at docs-writing time**: 19,269 links → 241 materials
  → **3 valid / 368 invalid**, runtime YAML `proxy:
  socks5://192.168.1.18:1090` (the inherited global trio — fine for exa, see
  the egress bullet). **The final yield line gets updated by the closeout pass**
  (plan T9: reconcile the `push_logs` row against hikari's
  `GET /api/keys?provider=exa` total growth, plus the double-probe freeness
  check). Do not restart/recreate `harvester-web` while it is live.
- Tests: `tests/test_exa_provider.py` (**87**), `tests/test_exa_pattern.py`
  (**8**), `tests/test_exa_rate_limit.py` (**3**),
  `tests/test_web_exa_push.py` (**36**), `tests/test_web_runner_exa.py` (**3**) =
  **137** new; full suite at merge = **1309 OK / 8 skipped** (`Ran 1309 tests in
  88.281s`, rc 0, `.omo/evidence/exa/t7-full-suite.log`) = the brave baseline
  1172 + exactly 137, 0 regressions, 0 `FAIL:`/`ERROR:` markers. Per-lane gates:
  ruff clean; pyright 0 errors (`provider/exa.py` carries the same single
  pre-existing `reportMissingModuleSource: requests` warning as
  `provider/tavily.py`); a 401-test all-provider neighbour run OK (the registry
  is shared state); a 244-test push/runner neighbourhood OK.
- Still open (plan gaps, not defects): T9's live reconciliation (`delta ==
  added`), the valid-key 422 and the probe-freeness double-probe; the live 401's
  `tag` field was never recorded (`t2`'s script logged only `error`) — bounded
  by design, since the 401 verdict is status-driven and both shapes classify
  identically; `added` is an estimate by construction (201-always) and a
  concurrent writer to the same pool skews it within `[0, accepted]`;
  `_seen_run_ids` grows for the process lifetime (shared with the sibling push
  services).
