# tavily provider + hikari push - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## tavily: what "验证一直报错" actually is (measured 2026-09-23, prod)

- **The error storm is a per-IP bulk-validation block, not key verdicts.** From
  any of our egresses, bulk `/usage` requests eventually answer
  `429 {"detail":{"error":"Your request has been blocked due to excessive
  requests"}}` — for GOOD and DEAD keys alike. Proof method: always run a
  control probe (a key known to answer 200) in the same window; if the control
  also 429s, the sample says nothing about the keys (measured: control 429 in
  1/5 probes during a burst).
- **The block follows cumulative distinct-key volume, not a fixed inter-request
  delay.** TavilyProxyManager's own hourly sync job (rq, 1276-key pool):
  `pace=0` → 20 ok / 247 failed; after setting `request_interval_seconds=6` →
  25 ok / 383 failed. Both >90% blocked. In contrast a 25-key sample at 6 s/key
  through fnos exit 1090 → 56% usable / 8% exhausted / 36% 429 / **0 dead**.
  So: small batches pass, a 1.3k-key sweep from one IP cannot, with or without
  pacing.
- **Consequence for the pool**: the sync job is the ONLY thing that flags dead
  keys (`quota_sync_service.go::syncKey`: 401 → `MarkInvalid`, 432/433 →
  `MarkExhausted`, 429 → **no state change**). Under a 429 block the pool keeps
  dead keys `is_active=true / is_invalid=false` and quota stays stale — which is
  why the proxy then serves `402` (exhausted key) and `503` (retry budget
  exhausted, 30 s timeout) to clients.
- **The harvester-side candidate pool IS mostly dead — a separate, genuine
  finding.** Run `415a13f9` (tavily, 09:00→11:25 CST, auto-picked exit 1091):
  123 valid / 2107 wait (1967 real-format, 140 junk) / 56 invalid / 39 no-quota,
  push_logs 123 keys → **7 added**. Replay of 65 sampled wait keys at polite
  pacing: **49% 401 dead, 34% alive-but-quota-exhausted, 14% usable** (≈275
  recoverable), 3% timeouts. Those dead keys sit in wait (not invalid) only
  because the throttled window answered 429 → RATE_LIMITED → wait.
- **Exits at measurement time**: 1080 / 1090 / 1091 (+1082) all healthy
  (good→200 `plan 100/1000`, dead→401); container-direct `api.tavily.com` still
  lossy (`ReadTimeout`). Re-check before blaming keys — blocks rotate.
- **Run-log noise is NOT tavily**: `ERROR` in `docker compose logs` =
  GitHub-side 404 (60/30 min) + 502 HTML (11) + the continuous
  `[GithubCrawl] GitHub API token rate limited` / `[github] all token
  credentials …` WARNING storm (all 7 tokens in cooldown 10:39→11:25), during
  which check-task generation stalled → tavily's counters froze. tavily check
  failures log at **DEBUG** only (`provider/tavily.py`), so they never appear as
  ERRORs.
- **TavilyProxyManager deployment & API** (ghcr.io/xuncv/tavilyproxymanager,
  rq `highkay@192.168.1.11` dir `/home/highkay/tavilyproxymanager`, port 48080,
  DB `data/proxy.db`, `settings.master_key` = the key harvester's
  `TAVILY_PROXY_AUTH_KEY` must match): `GET /api/keys` is **MASKED**
  (`util.MaskAPIKey` → `tvly-****abcd`) — never probe or diff those, use
  `GET /api/keys/export` (raw newline list); `GET|POST /api/keys/sync`
  (job status / trigger, returns per-key `status|error`); `DELETE
  /api/keys/invalid`; `PUT /api/settings/auto-sync` accepts
  `{enabled, interval_minutes(1-1440), request_interval_seconds(0-60)}`;
  `GET /api/stats` (`key_count`, `active_key_count`, `total_remaining`);
  `GET /api/logs/status-codes`. Its Go client uses `http.DefaultTransport`, so
  an `HTTP(S)_PROXY` env in its compose WOULD route upstream calls through
  another egress (not applied; unverified).
- **Applied 2026-09-23 on the proxy**: `request_interval_seconds` 0 → 6 and
  `interval_minutes` 60 → 360 (cuts the blocked-request storm ~6×; neither value
  makes the full-pool sync succeed). Revert = same PUT with the old values.
  Beware: with pace=6 a 1276-key sync needs ~2.1 h, so interval ≥ 180 keeps the
  job from running continuously.
- **Orbital noise (optional cleanup)**: the shipped pattern
  `(?:tvly|tavily)-[0-9A-Za-z_-]{20,}` also captures ~150 English prose tokens
  (`tavily-search-provider-plugin`, `tavily-web-search--research`) and ~140
  slug-shaped entries (`tvly-rec…`, `tvly-mcp…`, `tvly-test…`) per run — ≈7% of
  candidates. Authentic keys measured: `tvly-dev-` (1511 entries; 41 chars =
  `tvly-dev-` + 32) and `tvly-prod-` (5 valid, other widths) — narrow ONLY with
  a fresh corpus measurement and a tripwire, per the serpapi lesson.
- **Watchers deployed 2026-09-23 (all bounded, self-terminating)**: container
  `/tmp/tavily_validate_watch.py` → `/tmp/tavily_validate_watch.log` (120×5 min:
  bucket census, exit A/B, rotating 5-key wait replay); fnos host
  `/tmp/tavily_validate_host.sh` → `/tmp/tavily_validate_host.log`
  (search-side 429/5xx + GitHub-cooldown counts, tavily display row, check-task
  activity); rq `/tmp/tavily_proxy_watch.py` → `/tmp/tavily_proxy_watch.log`
  (sync job progress, auto-sync settings, pool stats, /search status mix).
  Delete by explicit filename; do not touch other sessions' files
  (`/tmp/recovered_*`, `/tmp/tavily_key_recovery.py`, `/app/data/harvest_watch.py`).
- **Security note**: the other session's container script
  `/tmp/tavily_key_recovery.py` hardcodes the proxy master key as a fallback
  default → rotate the master key (proxy `POST /api/settings/master-key/reset` +
  harvester `.env` `TAVILY_PROXY_AUTH_KEY`) or scrub that file.
  **Rotation caveat**: the harvester container's env is frozen at creation, so
  resetting the proxy key + editing `.env` WITHOUT a `docker compose up -d`
  leaves harvester-web pushing the old key — every tavily push then 401s
  silently (the push service never raises). And that recreate wipes the `cp`-ed
  code overlay → re-publish the whole tree (see the republish skill).

- **ROOT CAUSE of the client-visible 402s (found 2026-09-23, after the sweep
  classified the 402 keys as "usable")**: the upstream body is
  `{"detail":{"error":"Your account is currently disabled. This is likely due to
  unpaid pay-as-you-go balance. Please update your payment method or contact
  support@tavily.com"}}`, while `GET /usage` for the SAME key answers **200**
  with `plan_usage 0/1000 paygo_usage 0/20000` — **/usage cannot see a disabled
  account**. Verified end-to-end on pool id 3408: /usage 200 (plan 0/1000) vs
  /search 402 disabled. The harvester therefore classified such keys VALID,
  pushed them, and the proxy — whose own `GetUsage` also reads only
  key.usage/plan_limit levels — could not see it either, so every client
  request on such a key got a 402 with **no failover** (402 is absent from the
  failover switch). Measured in one 30-minute window: 16×402 + 3×503 vs **0×200**.
  Forensics shortcut: `request_logs.key_used` is the **pool key id** (not the key
  material), so one read-only SQL query names the offending keys; the 402s
  concentrated on ~19 ids (2789 / 3477 / 2560 / 3408 / 3623 / 2670 / …).
- **Fix (two-stage check, `provider/tavily.py`)**: stage 1 `_check_usage`
  (free) returns ONLY terminal `INVALID_KEY / NO_ACCESS / NO_QUOTA` — plan or
  per-key limit exhaustion — so spent keys never cost a search credit; stage 2
  `_check_search` POSTs a minimal probe
  (`{"query": "harvester key validation probe", "max_results": 1,
  "search_depth": "basic"}` = 1 credit) and judges the endpoint the pool actually
  serves: 402/432/433 (and disabled / pay-as-you-go / plan-limit markers) →
  NO_QUOTA, never valid, never pushed; 200 requires a JSON body **with a
  `results` array** (a 200 without it is a proxy interstitial → UNKNOWN).
  Pinned by `tests/test_tavily_provider.py::TestTavilySearchProbe` (the
  disabled-account case is the regression) and `TestJudgeSearch`.
  `/usage` stays the `inspect()` audit endpoint; `_is_quota_exhausted` stays the
  documented /usage quota rule used by ops tooling.
- The eviction sweep mirrors the same two-stage logic, so it evicts the
  disabled-account keys retroactively (`--from-402-logs N` targets exactly the
  ids behind the newest upstream 402 rows).
- **DEPLOYED 2026-09-23 18:09 CST** (user-directed restart; no recreate, so
  `.env` untouched): fnos aligned to `d0b11f3` (backup `rollback-20260923-180830`,
  0 local-only commits, `Dockerfile.web` restored + `docker-compose.hostnet.yml`
  re-applied), whole tree `docker compose cp`-ed, `docker compose restart`,
  `/health` = ok. Verified: md5 host↔container MATCH for
  provider/tavily.py · stage/definition.py · config/defaults.py ·
  examples/config-tavily.yaml · main.py · web/runner.py; in-container source
  markers (`_check_usage`/`_check_search`/`_judge_search`, the disabled-account
  regex, `not result.reason.is_retryable()`) all present; both tavily rate blocks
  read 0.2/2; defaults preset 0.2/2.
  **Functional proof** (`.omo/evidence/tavily_watch/post_deploy_proof.py`,
  deployed provider, exit 1090): known-good key → `available=True`
  ("Tavily /search probe accepted key"); pool id 3408 (the disabled account that
  used to classify VALID) → `available=False reason=no_quota`.
  Collateral, as predicted: nvidia / mimo-sg / qwen-intl were reconciled
  `failed: interrupted by service restart` at 10:10:26 UTC (= 18:10 CST); the
  in-container watcher died and was relaunched (new pid 106, banner 18:11:49);
  the host-side eviction loop survived untouched (verified 21:37: pid 3309405
  ppid=1, 21 cycles since 18:07, zero `container-unavailable` lines, host
  fallback file empty).
- **Post-restart salvage (executed 21:37 CST)**: pushes only fire on completion,
  and each provider's next cron backs up + resets `valid-keys.txt`
  (`auto_restore: false`), so the killed runs' validated keys would have been
  stranded. Replayed the documented salvage —
  `PushService.push_valid_keys(<provider>, <killed run id>)` with the 10:10:26-UTC
  failed row ids (`0e76292f…` nvidia, `65b6387a…` mimo-sg, `6619a9b4…` qwen-intl):
  **nvidia 401 keys → added 401 / ignored 0**, mimo-sg 2 → added 1, qwen-intl 1 →
  added 0 (already pooled). run this for ANY restart casualty whose provider has
  keys in its `valid-keys.txt`, before that provider's next cron fires.
- **Sustained verification after the deploy (21:37 CST)**: every `/search`
  through the proxy since the restart (08:10 UTC) is a 200 — hourly buckets
  98 / 158 / 95 / 18 / 20 = **389 rows, zero 402, zero 503 over ~4 h**; pool
  1276 total / 1230 active / 46 inactive.
- **Next relaunch of the hygiene loop must use the hardened copy**: the running
  instance keeps `/tmp/tavily_evict_loop.sh`; a hardened version (with
  `timeout 60` on the log/flush `docker compose exec` calls, so a blocking daemon
  call cannot wedge the loop before its cycle line is written) is staged at
  **`/tmp/tavily_evict_loop_v2.sh`** and in `.omo/evidence/tavily_watch/`.
  NEVER overwrite a script bash is currently executing — stage a new path.
- **Restart vs recreate for the container's /tmp pieces**: a `restart` preserves
  `/tmp` (the sweeper `/tmp/tavily_pool_evict.py` and the watcher
  `/tmp/tavily_watch_v2.py` survive — only their PROCESSES die and need
  relaunching); a **recreate** (`up -d`) wipes them, so after any recreate
  re-copy both from `.omo/evidence/tavily_watch/` BEFORE relaunching the host
  loop, or its remaining cycles fail through the whole tavily push window.
- **Durable hygiene loop**: `/tmp/tavily_evict_loop.sh` →
  `data/tavily_evict_loop.log` — every 10 minutes it re-reads the newest
  upstream 402 rows (`--from-402-logs 200`) and evicts exactly those ids (12
  cycles = 2 h; the sweep picks them up on its own, e.g. 2560/2789/2520/3408/
  3477/3623/3624). Three traps: `data/` is root-owned, so a host-side `>>`
  redirect fails with EACCES — append through
  `docker compose exec -T harvester-web sh -c 'cat >> /app/data/…'` (with a
  `/tmp` host fallback, since a failed exec is otherwise silent); keep the
  sweep's `--threads` ≤4 so its Resin rotation does not starve the harvester's
  own probes; and before restarting the loop, confirm no `tavily_pool_evict.py`
  is mid-round (grep for a `round N start` without its `round N end`) so an
  `--apply` cycle is not cut in half. It also separates "dead container" from
  "quiet round": under `set -o pipefail`, a failed/empty `docker compose exec`
  logs `cyc=N container-unavailable/exec-failed (rc=…, skipped) — NOT a
  zero-target round`, so a restart window cannot be misread as "0 new 402 ids".
- **Effect verified, time-bounded**: every `/search` row since the first
  eviction (06:41:38 UTC) is **200** — 24 rows, zero 402, zero 503 (the attempt
  count behind them is NOT readable from those rows; see the next-but-one
  bullet). The 3×503 quoted from the "last 10 minutes" window were pre-cutoff
  rows (06:35), so that window could not have shown the fix.
- **Coverage, not "fully fixed"**: 43 pool keys have been deactivated this
  session — the 16 behind the newest upstream-402 rows (2520 2560 2670 2690 2698
  2785 2789 2807 3128 3129 3408 3477 3563 3586 3623 3624) plus 27 spent-counter
  keys from the `--order quota` pass; the loop re-reads the 402 rows every 10
  minutes and currently finds 0 new targets. The remaining ~1233 keys were NOT
  swept (a full pass is hours out by design: the sweep targets the keys clients
  are actually failing on, it is not exhaustive). Re-check the discriminator
  after any future 402: its `key_used` must not be an id already set
  `is_active=false` — that would mean the proxy's selection ignores the flag.
- **Attempt count: OPEN, and rows cannot answer it (2026-09-23 15:2x)**. In
  `tavily_proxy.go`'s candidate loop every non-terminal branch (transport error,
  401 → MarkInvalid, 429 → stash lastRateLimited, 432/433 → MarkExhausted)
  `continue`s with only a structured `logger.Warn("upstream request failed" /
  "key rate limited" / "key marked invalid" / "key quota exhausted", request_id=…)`;
  the keyed `logs.Create` sits on the terminal path only. So **`n=1` rows per
  `request_id` is guaranteed for every successful request no matter how many
  keys were burned** — it is not evidence about failover in either direction,
  and the earlier "single upstream attempt" reading (and the "do not resurrect
  the failover explanation" directive) must be disregarded.
  The attempt count lives in those warns — but **the deployed container emits no
  slog output at all**: in 24 h it logged 0 × `proxy request completed` (which
  fires on every success) and 0 of the four warns, only GIN + GORM lines. So the
  question stays open until the container's logging is fixed (set
  `LOG_LEVEL`/`LOG_DIR` in its compose or move off the `:main` image) and the
  warns are correlated by `request_id` against a slow GIN row.
- **Latency measurements (same window)**: controlled probes through the proxy
  (3× `/search`, `max_results=1`) show client-observed 1.367 / 1.947 / 2.801 s
  vs logged `latency_ms` 1332 / 1908 / 2764 ms — for normal requests the logged
  upstream leg ≈ what the client waits. Individual logged legs do reach
  2.7-12.1 s and GIN rows up to 18.6 s exist (06:47), but those two sets cannot
  be paired by timestamp under concurrent traffic: any residual gap is
  **unresolved** (see the logging gap above), not "upstream slow" and not
  "proxy overhead".
- **`stats.active_key_count` semantics and the stale-counter keys** (verified in
  code, not inferred): `KeyService.Candidates` (ks.go) and the stats count both
  use `is_active = true AND is_invalid = false AND used_quota < total_quota`, so
  the proxy really does skip keys whose STALE counters read spent (measured
  1260 active vs 1242 with quota = 18 such keys). A `--order quota` pass over the
  40 highest-stale-`used_quota` keys settled them: **27 exhausted → deactivated**
  (plan 1000/1000, one with 432 "exceeds your plan's set usage limit"), 7 usable
  → counters refreshed, 6 transport errors (retried next round) — active count
  1260 → ~1233. Never quote `active_key_count` as `COUNT(is_active=1)`.
- **Watcher guard**: a wait-bucket replay is published only when the chosen
  exit's OWN control probe answers 200; otherwise the cycle logs
  `replay[…] SKIPPED — exit-limited (control=403…)`. Verified live 15:26
  (`stats_active=1231`, exit 1091 control=403 → SKIPPED). Rationale: exit 1091
  answered `403 Forbidden` HTML to a known-good key AND a dead key at once,
  so anything it returns that cycle is exit-artifact, not a key verdict.
- **Pidfile guard trap (cost a dead watcher at 15:11-15:26)**: the guard is
  `os.kill(pid, 0)` on the pidfile, so a STALE pidfile whose pid got reused makes
  the new instance exit instantly with `watcher already running` — and that line
  goes to `/app/data/tavily_validate_watch.out`, not the log, so the log simply
  stops advancing. When (re)starting any pidfile-guarded watcher: `rm -f` the
  pidfile first, then confirm a NEW `started … (pid N)` banner and one logged
  cycle. The 5-minute cadence makes a dead watcher look merely quiet.
- **Restart deadlines — CLOSED 2026-09-23 18:09 CST**: the code fix is live (see
  the DEPLOYED bullet above), so the exposures below no longer apply. Kept for
  the record: before the deploy, the pool was protected only by the eviction loop
  and the next tavily run (09:00 CST, push ≈11:25) would have validated with the
  PRE-fix provider; the sibling session's wait recovery (~13:53) pushed under
  that same pre-fix provider.
- **Resin probe discipline inside the loop**: a cycle's `ab[resin]` can read
  `EXC-ReadTimeout` for BOTH keys — that is one dead node, i.e. noise (≈75%
  per-probe success, retry on another account before believing it). Worth knowing
  that with 0 targets the sweep's Resin path is untested in that cycle, and if
  new 402 ids appear while Resin is flaky the sweep files them as `error` and
  retries next round rather than evicting — safe, but hygiene degrades silently.
- **Slowest logged leg observed: 14 395 ms** (a 200 at 07:23:32 UTC, single row)
  — supersedes the earlier 12.1 s figure. The attempt-count question stays OPEN
  (logger sink), see above.
- **Restart checklist (executed 18:09 CST; kept for the next one)**. Window =
  `SELECT provider_name FROM run_records WHERE status='running'` returns nothing
  (the 18:09 restart was user-directed with nvidia / mimo-sg / qwen-intl live —
  all three were reconciled `failed: interrupted by service restart` at
  10:10:26 UTC, so budget for that when planning one yourself). A restart also
  kills any in-container jobs (the sibling's wait recovery did NOT appear in the
  process list by then, but it ran earlier — report, never take over) and the
  watcher PROCESS — relaunch `/tmp/tavily_watch_v2.py`
  (`TAVILY_WATCH_CYCLES=120`, `RESIN_PROXY_TOKEN`; `rm -f` the pidfile first,
  then confirm a NEW banner + pid and a fresh cycle). The host-side
  `/tmp/tavily_evict_loop.sh` (`CYCLES=130`, banner prints its expiry) survives a
  restart untouched; after a RECREATE re-copy the two container scripts instead
  (see the restart-vs-recreate bullet above).
- **Deploy pre-validated**: a clean worktree at the deployed SHA `d0b11f3` runs
  **789 tests OK / 8 skipped**.
- **fnos tree alignment is a POINT-IN-TIME fact, not a standing state** (this is
  exactly what produced a stale `1.0/5` copy earlier): the tree was reset to
  `d0b11f3` and the container files re-copied FROM it at 18:09 CST, with md5
  MATCH verified for the six key files — but other sessions push to `origin/main`
  directly, so BEFORE any future republish re-run step 1 of the recipe
  (`git fetch origin` + empty `origin/main..HEAD` + `git reset --hard origin/main`
  + restore `Dockerfile.web` + re-apply `docker-compose.hostnet.yml`) and
  re-verify the md5s instead of trusting this note.
- **Exit blocks change flavour**: at 14:48 the SAME exit 1091 answered
  `403 Forbidden` HTML on /usage for a known-good key AND a dead key at once —
  the per-IP block rotates between `429 blocked due to excessive requests` and a
  plain 403 interstitial. The discriminator is unchanged (control + dead key must
  read 200/401; anything else is the exit, not the key), and `_judge_usage` maps
  403 → NO_ACCESS → wait-check, i.e. recoverable rather than a permanent discard.
- **Deploy state (2026-09-23 ~14:55)**: the three `examples/config-tavily*.yaml`
  were `docker compose cp`-ed into the container after diff-verifying that ONLY
  the rate change differs from the deployed copies — configs are read at run
  start, so the 0.2/2 pace is live for the next run WITHOUT a restart. Still
  needing the safe-window restart/recreate: `provider/tavily.py` (two-stage
  check), `stage/definition.py` (limiter feedback), `config/defaults.py` (an
  imported module), and any `.env` change (`HARVESTER_PROXY_TAVILY`).

### Resin: the pool's clean egress (measured 2026-09-23)

- **Resin** (`resinat/resin`) runs on fnos as container `resin`, dir
  `/home/admin/resin`, port **2260**; an HTTP/SOCKS5 forward proxy + reverse
  proxy over a node pool with **sticky sessions per `Platform.Account`**
  (state at `data/state/state.db`: endpoints / platforms / subscriptions /
  account_header_rules). Its compose holds `RESIN_ADMIN_TOKEN` /
  `RESIN_PROXY_TOKEN`.
- **Auth forms** (all measured): `-U "Default.<acct>:TOKEN"` for HTTP forward
  (and the same for SOCKS5 per its README); the empty-account form `-U ":TOKEN"`
  **rotates per request** — curl honours it, but Python `requests`/urllib3 DROP
  credentials for `http://:pass@host` (ProxyError) — so from Python use the
  non-empty form `http://Default.:TOKEN@192.168.1.18:2260` (Platform `Default`
  + empty Account), which also rotates per request. 8 accounts × 1 probe →
  8 distinct egress IPs; the same account 4× concurrently → 1 IP (stickiness
  confirmed).
- **Success rate per probe ≈ 75%** (measured 5/8, 3/4, 4/6 …): individual nodes
  time out, so ALWAYS retry on a different account before treating a probe as a
  verdict — one dead node is noise. This is the cheapest way to dilute tavily's
  per-IP distinct-key block for paced sweeps/recoveries.
- Harvester-side egress override (NOT yet applied — needs the safe-window
  recreate, see below): `HARVESTER_PROXY_TAVILY=http://Default.:<TOKEN>@192.168.1.18:2260`
  gives tavily scans per-request rotation.

### tavily: fixes applied 2026-09-23 (repo + prod)

- **Rate policy**: `examples/config-tavily*.yaml` (all three) and the
  `config/defaults.py` tavily preset moved from 1.0-2.0 req/s to **0.2/2**
  (1 per 5 s), pinned by `tests/test_tavily_rate_limit.py` — the defaults↔
  examples lockstep test. Rationale: 1-2 req/s through one exit is the measured
  trip point; the hardened providers already ran at 0.2/2.
- **Limiter feedback bug (fixed)**: `CheckStage._check_worker` reported
  `report_result(service_type, True)` unconditionally, so `adjust_rate`'s
  failure branch (halve after 3 consecutive failures) was unreachable and the
  bucket could only accelerate toward 2× base. It now reports
  `not result.reason.is_retryable()`, pinned by
  `tests/test_check_stage_verdicts.py::TestCheckStageLimiterFeedback`
  (RATE_LIMITED ×3 halves the bucket; INVALID_KEY/success do not back off).
- **Proxy auto-sync DISABLED** (`PUT /api/settings/auto-sync {"enabled":false}`):
  the full-pool sync is ~98% blocked from the rq egress (1249/1251 × 429, at
  both 0 s and 6 s pacing) and it cannot see `account.plan_usage` anyway, so it
  only produced the error storm. Pool hygiene now runs as a paced external
  sweep. Values kept for later: `request_interval_seconds` 4-6, `interval_minutes`
  360.
- **Eviction sweep** (the actual 402 fix): source
  `.omo/evidence/tavily_watch/tavily_pool_evict.py`, deployed in the container as
  `/tmp/tavily_pool_evict.py`; state + log on the bind mount
  (`data/tavily_pool_evict_state.json` / `.log`) so recreates cannot wipe them.
  Resumes; rotates 12 Resin accounts (`Default.a1..a12`); 1.5 s pace; retries a
  transport error on another account (3 attempts) before filing anything;
  control probe = classify a known-good key on THREE accounts before believing
  an egress-limited window. `--order recent` walks the recently-used keys first
  (those are the ones the proxy hands to clients and the only ones that can
  402). Actions: `is_active=false` for 401-dead / `_is_quota_exhausted` keys;
  counter refresh (`used_quota`/`total_quota` from `account.plan_usage/plan_limit`)
  for usable keys, because the proxy's own accounting cannot read plan-level
  usage. Verified live: `id=2807 exhausted plan=1000/1000 → is_active=false`,
  `active_key_count` 1261 → 1259 → … (first 38 keys were all usable: the pool is
  NOT mostly dead — the 402s come from a spent minority).
- **Dead-key wording correction**: it is NOT true that the sync is the only
  path that flags keys — live traffic (`tavily_proxy.go::Do`) also marks
  `401 → MarkInvalid` / `432/433 → MarkExhausted`. The precise gap is that
  **402 is handled by neither path and is absent from the failover switch**, so
  it is returned to the client verbatim.
- **Deployment mechanics for these fixes**: `examples/*.yaml` + `config/*.py`
  changes land with `docker compose cp` and take effect on the next run (configs
  are read at run start — no restart needed); `stage/definition.py` needs a
  restart; an `.env` env change (e.g. `HARVESTER_PROXY_TAVILY`) needs a recreate
  → do all three in one safe-window pass (no `run_records` row in `running`),
  republishing the whole tree afterwards.
- **Watchers (corrected, 2026-09-23)**: container
  `/tmp/tavily_watch_v2.py` → **bind-mount** `data/tavily_validate_watch.log`
  (+ `.pid` guard; the image has no `pgrep`): census, exit A/B (1091/1080/1090 +
  Resin), stratified wait replay (head/middle/tail thirds) with a control probe
  and pipeline-rule classification, plus proxy pool metrics (`/api/stats` +
  sync job). rq `/tmp/tavily_proxy_watch.py`: fix the GIN parse — **parts[1] is
  the status and parts[2] the latency**; filter to `POST` + `"/search"` or
  healthz/stats 200s inflate the success rate. A sibling session runs its own
  wait-pool recovery (`/tmp/tavily_wait_recovery.py`, ≥5 s/key, exports
  1090/1091) — monitor it, do NOT start a second recovery over the same bucket.

## Feature: tavily + serpapi pushes now feed tavily-hikari (2026-10-15)

User-directed migration (scope confirmed: BOTH providers): the harvester's
tavily and serpapi pushes now target the **tavily-hikari** multi-provider pool
on fnos (`http://192.168.1.18:8787`; container `tavily-hikari`, image
`ghcr.io/highkay/tavily-hikari:latest`, `DEV_OPEN_ADMIN=true`). Env names are
unchanged — all four `TAVILY_PROXY_*` / `SERPAPI_PROXY_*` vars in fnos and
workstation `.env` point at hikari with the shared `th-…` client token (token
only ever in gitignored `.env` / runtime env, never in git). The OLD targets
are no longer fed: TavilyProxyManager (rq `192.168.1.11:48080`) and the
standalone serpapi-proxy (fnos `48081`, 647-key pool still serving).

- **Contracts** (from `F:\git\tavily-hikari\docs\api-reference.md`, 1394 lines,
  source-cited to HEAD `2024b562`; the deployed build
  `0.0.0-fork.20261014.2024b56` is that SHA):
  * tavily: `POST {base}/api/keys/batch`, body `{"api_keys":[...]}` — batch is
    Tavily-only; a present `items` key beats `api_keys`; limit 1000 → chunk
    500; HTTP 200 + per-item `status` (created/undeleted → added,
    existed/duplicate_in_input → ignored, failed → failure with `error`); a 200
    covering fewer items than submitted counts the missing as failures.
  * serpapi: per-key `POST {base}/api/keys` body `{"api_key":...,
    "provider":"serpapi"}`; add-or-undelete → **201 for both new and existing**
    (measured) → net-new via the provider-scoped total delta around the loop;
    unreadable totals fall back to accepted-count + a note in
    `push_logs.error_message`.
  * Auth: the admin realm has NO Bearer channel; `DEV_OPEN_ADMIN=true` makes
    admin calls credential-free (the `th-…` token is a client search token —
    `GET /api/tavily/usage` 200 — and the Bearer header is ignored today). If
    the instance switches to builtin/forward-auth admin, pushes will 403 →
    push_logs failed until the harness sends the right credential.
- **Deployed 2026-10-15 (`205656c`)**: recreate for env + whole-tree cp +
  restart; md5 host↔container MATCH for `web/tavily_push.py` /
  `web/serpapi_push.py`; `/health` ok; the live tavily run was reconciled
  `failed: interrupted by service restart` and pushed through the NEW service
  (startup recovery `added=20`, hikari tavily 2040→2060; idempotent manual
  re-push `added=0`); serpapi's last run backfilled (`added=213`; hikari
  serpapi 415→628 — the migration had left 213 keys unpooled). Evidence:
  `.omo/evidence/ulw-hikari-push/`.
- **Watch**: the TavilyProxyManager/tavily-proxy ops sections above describe the
  OLD target (history — do not apply to hikari). `docker compose cp` overlay +
  restart discipline unchanged.
