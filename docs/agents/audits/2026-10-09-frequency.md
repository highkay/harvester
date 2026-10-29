# audit 2026-10-09 (frequency policy + Fastly probe) - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Ops: 2026-10-09 audit → frequency policy + fleet Fastly-probe fix

User-directed review of prod logs/data ("大量重复 key 是否合理？qwen 等停用是否合理？").
Verdict: **the records are self-consistent; duplicates are by design; the 09-28
cull is justified by the data.** Evidence + the two changes that came out of it:

- **Duplicate-key forensics (11 days, backup-set diffs)**: cross-run overlap is
  98-100% for every enabled provider (modelscope literally 59→59→59 with 0 new
  for 3 straight days; serpapi 99.3-99.8%; tavily 99.7%). push_logs since 09-28:
  nvidia 75 added / 10,892 ignored; serpapi 10 / 7,625. Mechanism: GitHub code
  search is a stable best-match ranking over a plateaued corpus, and
  `auto_restore: false` re-validates the same corpus each run — push-side dedup
  (ignored) is the designed absorption. **The duplicates are not a defect; the
  defect would be paying daily quota for them** → frequency policy below.
  Per-provider daily churn that JUSTIFIES staying daily: nvidia +7-15 new /
  7-14 dead, tavily +4-11 / 15-30 dead, glm +5-6, glm-ai +1-7 (pool refill ≈
  pool death; nvidia pool 2072 and glm pool 552 stayed flat 09-29→10-09).
- **tavily added-spikes 10-01 (+188) / 10-02 (+451) EXPLAINED — monthly quota
  reset, not a bug**: tavily valid jumped 878 (09-30) → 1842 (10-02) overnight
  and stayed ~1800. Plan-exhausted keys (plan_usage 1000/1000, evicted/no-quota
  in September) revived when the monthly plan reset on 10-01, re-validated, and
  re-entered the pool as "added". The +188 row was the startup recovery push of
  the restart-killed 10-01 run (`44fe43b0`) — lifecycle hardening working as
  designed. Expect this every month turn: a burst of "added" + a ~2x valid count
  for tavily in the first days of the month.
- **Frequency policy applied on prod 2026-10-09 (PUT /api/schedule, hot)**:
  modelscope `0 3 * * wed` (weekly), ollama `0 3 * * mon,thu`, openrouter
  `0 8 * * mon,thu`, agnes-ai `0 10 * * tue,fri`, serpapi
  `0 6 * * mon,wed,fri` (mild churn, 3x/week; hour 05→06 avoids the glm-ai
  daily 05:00 on shared days). Unchanged
  daily: tavily 09, nvidia 11:50, glm 12, glm-ai 13. **House rule going
  forward: a cold/saturated provider gets a WEEKLY FLOOR, never full disable**
  (user decision 2026-10-09) — staleness law means supply can resume any week,
  and a weekly probe is bounded-quota insurance against permanent blindness.
  opencode stays weekly under this rule (3rd consecutive 0-valid run on
  10-04, 95,830 links — if it never yields, weekly is still the floor). The 11
  disabled providers (qwen×2/kimi×3/mimo×2/deepseek/github/groq/cerebras) stay
  disabled — the 09-28 evidence stands (qwen-cn 326k links → +10; qwen-intl
  186k → +0, pool now 0 keys; deepseek 691k → +3; github 1.03M → 3). Seed list
  in `web/scheduler.py` synced to the new crons (pinned by test_web_scheduler).
- **APScheduler day-of-week FOOTGUN (found in the same-day effect review, fixed
  2026-10-09)**: `CronTrigger.from_crontab` passes the weekday field straight
  into APScheduler's NATIVE numbering — **0=mon … 5=sat, 6=sun**, NOT standard
  crontab's 0=sun. The first (numeric) version of the policy therefore fired
  one weekday off (`1,3,5` = tue/thu/sat instead of mon/wed/fri; next_run_time
  in the PUT responses exposed it: serpapi landed on Sat 10-10). Worse,
  opencode's `0 22 * * 6` had been firing on **SUNDAY** since 09-28 while every
  doc said Saturday (the 10-04 run was a Sunday). Fix: all weekday-carrying
  crons use NAME form (`mon,wed,fri`) on prod + in the seed list; semantics
  pinned by `test_dow_crons_fire_on_intended_weekdays` (rejects numeric dow in
  seeds and asserts actual fire weekdays == intended). When auditing "why did X
  fire on the wrong day", check this first — `run_records.started_at` weekdays
  vs the cron's numeric dow are the tell.
- **10-03→10-05 global 6-9x slowdown ROOT-CAUSED: warp-go Fastly-leg
  degradation + health-checker blindness**. Log census per day (main.log
  rotations): SOCKS transport errors 0→490/700/452, read timeouts 0→358/405/337,
  proxy failovers 0→130/216/152 per day across 10-03/04/05; back to 21/7 on
  10-06 and ZERO on 10-07/08. All providers slowed simultaneously (even 16k-link
  agnes-ai 6→54 min) = shared egress, not corpus. Harvester code behaved
  correctly (failover/timeout-floor/retries all active; **zero yield loss** —
  valid counts on the slow days match healthy days). Recovery correlates with
  staged node restarts: wg13 (10-04), wg01/02/03/07/10/12 (10-05), wg08/09
  (10-06) — `docker ps` uptimes match. The checker
  (`/home/admin/warpgo-fleet/scripts/check_warpgo_fleet.sh`, systemd-user timer
  every 5 min) probed ONLY `www.cloudflare.com`, so the Fastly-leg degradation
  (the 09-24-documented blind spot) was invisible until cloudflare probes also
  started failing days later.
- **FIXED (fleet side, deployed+verified 2026-10-09 11:37 CST)**: the checker
  now runs a SECONDARY probe per node — `CONNECT raw.githubusercontent.com:443`
  → TLS → `GET /` (any HTTP status line = pass; it tests tunnel reachability,
  404 is fine) — only when the primary cloudflare probe PASSES, with its own
  consecutive-fail counter (`RAW_FAIL_THRESHOLD=4` ≈ 20 min at the 5-min
  cadence, deliberately more conservative than the primary's 3) sharing
  RESTART_GRACE / MAX_RESTARTS_PER_RUN / the global-outage flag / last-restart
  state. Raw-path fails do NOT feed `fails_this_run` (global-outage semantics
  stay "the node stack is down"): with the primary passing, a dead Fastly leg
  is exactly what a node restart fixes. Backup:
  `check_warpgo_fleet.sh.bak-20261009-113628`. Manual run + the next timer run
  both passed 14/14 UP, 0 restarts. Report lines now distinguish `UP` /
  `UP raw:FAIL(n/4)` / `RESTARTED(raw)`. **Do not "simplify" the two-probe
  structure back to one target** — the single-target version is what let a
  3-day, 6x degradation run invisible.
- **Watch item (not fixed, environmental)**: if the whole fleet's Fastly leg
  degrades at once (upstream WARP/IPv6 routing), per-node restarts are capped at
  3/run and may churn without fixing it — the harvester-side mitigations
  (failover rotation, 20 s timeout floor, retries) are the backstop, and they
  held with zero data loss in the 10-03 incident. Re-check
  `docker compose logs` SOCKS-error census if run durations inflate together
  again (recipe: bucket main.log by day, count SOCKSHTTPSConnectionPool /
  Proxy failover / Read timed out).
- **Deep review the same afternoon (4 parallel evidence agents: scheduler/
  lifecycle, push/pools, storage/disk, code review)** — system verdict HEALTHY:
  21 schedule rows correct with intended next_run weekdays; 0 NULL-duration
  failed rows; 8 restart-interrupted rows since 09-28 all map to documented
  deploy windows (ZERO organic failures); 0 push failures since 09-28 (the two
  agnes rows are the pre-fix 09-28 WAN class); fnos pool counts match records
  exactly (nvidia 2072 / glm 552 / zai 72 / ollama 71 / openrouter 112 /
  modelscope 63 / opencode 0); rn agnes pool 79 ≡ push added 6; tavily proxy
  ZERO 402/503 in the last 7d (last 402 09-28 from the deactivated key 2746);
  evict loop alive (cyc≈1279, 0 container-unavailable) with KillMode=process
  watchdog; harvester.db 2.3 MB quick_check ok (journal=delete, no WAL);
  links.db write-gate holding at 36 KB; active providers' backups all ≤3;
  today's ERRORs are 99.4% the known GitHub-429 search-budget ceiling;
  false-404 fixes holding (~10/day vs 3484/48h pre-fix).
- **Deep-review FIXES (this commit set)**: (1) **add-time token validation
  misread rate-limited 403s as invalid** — GitHub answers 403 for a VALID
  token under primary (`x-ratelimit-remaining: 0`) and secondary (`Retry-After`
  / "secondary rate limit") limits, and the pool routinely sits at the 10
  req/min ceiling, so adding a token during a limit window spuriously rejected
  it (`web/token_service.py::_validate_token_gh` now routes rate-limit-marked
  403s to "unknown" → stored with NULL expiry; 401 and genuine 403 stay
  invalid; mirrors the runtime's marker set). (2) **`add_token`/
  `add_tokens_bulk` blocked the event loop** — the sync `requests.get` probe
  (15 s timeout) inside `async def` froze the whole web app per token; now via
  `asyncio.to_thread` (bulk stays sequential on purpose — parallel probes
  would hammer the shared budget). (3) **strike→dead race** —
  `_record_auth_failure`'s two-call sequence (`mark_strike` then `mark_dead`,
  separate locks) let a concurrent `mark_alive` from an in-flight success slip
  between them and quarantine a token that had just succeeded; `mark_strike`
  now takes `dead_threshold` and does count+quarantine in ONE lock
  acquisition. All pinned by tests (web token health 5 new, runtime 2 new;
  suite 1038 OK / 8 skipped).
- **Deep-review watch items — ALL CLOSED the same day (user directive: no
  deferred items, fix at the root)**:
  * **Log-deletion trap KILLED AT THE ROOT**: `DEFAULT_LOG_CLEANUP_DELETE`
    (compile-time True) became a tri-state `LOG_CLEANUP_MODE` env
    (`constant/system.py`): `keep` (DEFAULT — logger init never touches
    existing files), `delete` / `archive` (legacy CLI fresh-run behaviour,
    opt-in). A probe process importing the logger can no longer unlink the
    live app's sinks — the 09-29/09-30 forensic-gap class is structurally
    impossible now. Web restarts also APPEND main.log across restarts
    (better forensics); age is bounded by the cleanup below. CLI behaviour
    change is documented (set HARVESTER_LOG_CLEANUP_MODE=delete for fresh
    logs per run). Pinned by `tests/test_logger_cleanup_mode.py` (10).
  * **>7d log accumulation FIXED without relying on restarts**: the web
    lifespan now owns a daily `_log_cleanup_loop` asyncio task
    (`web/app.py`, interval env `HARVESTER_LOG_CLEANUP_INTERVAL_SECONDS`,
    default 86400) re-running `cleanup_old_logs(days=7)`; a failed sweep
    logs and the loop survives. Pinned by `tests/test_web_log_cleanup_loop.py`
    (2, wall-clock-bounded per the py3.12 sleep-race lesson). The existing
    12 stale files + 4 deleted inodes (~10 MB) clear at the 10-09 safe-window
    restart.
  * **Frozen-provider backup piles FIXED structurally**: the per-scan prune
    never ran for disabled providers (6 of them held 4-7 backup dirs,
    ~400 MB). `PipelineRunner.__init__` now calls
    `_prune_all_provider_backups()` (delegates to the single-sourced
    `storage.persistence.prune_backup_dirs`, never raises), so EVERY restart
    enforces retention across all 22 provider dirs. Pinned by
    `TestStartupBackupPrune` (3) in test_web_runner_lifecycle.py; the frozen
    piles themselves are pruned by the 10-09 safe-window restart.
  * **serpapi `searches_left` drawdown (355k→179k) ROOT-CAUSED — external,
    not our leak**: the deployed refresher (`serpapi_proxy/app.py
    _refresher_loop` → `upstream.check_account`) ONLY calls
    `serpapi.com/account.json`, which is quota-exempt (code re-verified
    2026-10-09); client forwarding used 4 keys / ~486 req over 7d. The
    drawdown is account-side consumption by OTHER holders of the same leaked
    accounts (total_searches_left is account-wide). Pool remains ~370x the
    client load; no action possible or needed beyond the recorded threshold
    guidance (re-audit if active <100 or searches_left <10k).
  * **responses.db 543 MB high-water**: VACUUM'd (~71 MB free pages
    reclaimed) in the 10-09 safe-window stop phase; the ~470 MB live set is
    the legitimate 1000-row search-JSON working cache, bounded by the row
    cap — not a leak, no config change.
  * **Runtime orphan (Oct-1 tavily) + rollback-dir accumulation + stray
    empty dir**: orphan swept by the restart's startup sweep; rollback-*
    pruned to the latest two and the stray `' '` dir removed manually
    (logged); repo-root .bak files deliberately kept (user-created).
- **Safe-window restart discipline (10-09 round 2)**: the restart that
  activates the token-health fixes + the three mechanisms above was NOT taken
  ad-hoc — a guarded host-side script waits for `run_records` running==0
  (bounded window, 5-min polls), then `docker compose stop` → VACUUM
  responses.db (guarded by PRAGMA quick_check, sizes logged) → `docker
  compose start` → verifies /health, startup log lines (orphan sweep, backup
  prune, log cleanup) and zero running rows, logging every step to
  `/home/admin/safe_restart_20261009.log`. Rationale: an immediate restart
  would have killed the live nvidia (~2h in) + glm-ai runs for fixes whose
  triggers are low-frequency; the script converts "deferred" into "scheduled
  with guards".


