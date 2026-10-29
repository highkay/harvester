# egress routing (raw direct, sized pool, exit failover) - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Feature: egress routing — raw direct, sized pool, exit failover (2026-09-24)

**Incident**: 15 rows sat in `running` for 7-20 h with near-zero yield. Measured
on production that evening: `main.log` carried **340,504 `SOCKSHTTPSConnectionPool`
errors in 28 h** (325,044 of them to `raw.githubusercontent.com`, ~1.2k/h to
`api.github.com`); `retry.log` failure mix = Read timed out 19,864 /
ConnectTimeout 6,210 / Connection refused 3,138 / SSL EOF 828 / Host unreachable
284; search logged ~2-20 completions per HOUR across all runs. Per-node traffic
census: `warpgo-wg08` (port 1090) took **269 SOCKS CONNECTs/60 s while the other
13 warp-go nodes took 0-6** — i.e. 100% of the harvester's GitHub traffic was
funnelled through ONE WARP exit. Probes from inside the container: raw fetched
**direct 8/8 @0.6 s** (16/16 and 32/32 at higher concurrency, p50 0.4 s) but
**0-1/8 through the socks exits** (12-20 s or timeout); `api.github.com` direct =
TLS reset, 1091 = 2/2 @1.8-2.8 s, 1090 = 0/2. Runs never drained because every
link cost 3 × 10 s connect timeouts + backoff + a stage requeue (~2 min worst
case) against 1.3k-4.8k-deep gather queues, while each hourly chain tick added a
new run; the stranded rows also hold the scheduler guard
(`already running — skipping`, measured for github at 18:50).

**Mechanism** (all three parts had to line up):
1. `manager/pipeline.py` constructs a Pipeline per scan and calls
   `client.set_proxy(config.global_config.proxy)` — a **process-wide** session.
   Every new scan overwrote the exit for ALL in-flight scans, so
   `web/runner.py`'s "one proxy per scan so concurrent scans spread across
   proxies" comment was false for overlapping runs (candidates were spread in
   the per-run runtime YAMLs, effective traffic was one node).
2. `use_proxy` is a **provider-validation-only** knob (`provider/base.py:220`,
   wired in `manager/task.py:140`): search/gather never received it, and
   `_collect` → `http_get` → `request` defaulted to the proxied session. Before
   2026-09-23 that was correct (gather fetched `github.com/.../blob/...`, which
   needs the proxy); the raw-bytes fix moved the target to
   `raw.githubusercontent.com`, which does not — but the call path kept the
   proxy hop, so gather inherited the exit's failure mode.
3. There was no exit failover and the exit is fixed for the run's lifetime.

**Fix (this commit, `search/client.py` + `tests/test_client_egress_routing.py`)**:
- `_DIRECT_HOSTS = {"raw.githubusercontent.com"}` with `effective_use_proxy()`:
  every call path (gather, cache revalidation, future callers) bypasses the
  proxy for those hosts; `effective_timeout()` applies a **20 s floor**
  (`_DIRECT_TIMEOUT_FLOOR`) because the measured 32-way tail (~16 s) exceeded
  the 10 s default.
- `_new_session()` mounts `HTTPAdapter(pool_connections=32, pool_maxsize=64,
  pool_block=True)`: requests' default 10-connection pool with `block=False`
  churns a throwaway connection per overflow request, which is the signature
  behind mass `ConnectTimeoutError` / "Host unreachable" while a single request
  succeeds (15 runs × 4 gather workers ≈ 60 concurrent).
- `_note_proxy_transport()` + `proxy_rotation()`: the proxied session rotates to
  the next `HARVESTER_PROXY` candidate after **3 consecutive transport
  failures** (any HTTP answer resets the streak, so a busy-but-alive exit is
  never abandoned; logs are masked — pool proxies carry userinfo tokens).
- `GitHubClient._http_get()` now goes through `request()` instead of
  `_HTTP_SESSION.request()` directly, so the search leg inherits routing,
  timeout floor and failover (`raise_for_status` only fires for 4xx/5xx, not
  304). Probe helpers: `client.get_egress_state()`, `effective_use_proxy(url)`.

**Tripwires / follow-ups**:
- `use_proxy: false` in a provider config still only affects validation — if a
  gather host must bypass the proxy, add it to `_DIRECT_HOSTS` (and re-measure
  first, per the serpapi/glm lesson).
- The failover is process-wide and re-pinned by each new scan's `set_proxy`; the
  per-run session isolation is still the open workstream (see the
  "deliberately left open" note in the 2026-09-23 hardening section of `AGENTS.md`).
- `HARVESTER_PROXY` widening is pointless until per-run sessions exist — one
  exit is live at a time, which is exactly what the census showed.
- Fleet side (not harvester code): warp-go runs `-ip 6`, wg08's log shows tunnels
  to Fastly IPv6 (`2606:50c0:800x::154`) timing out while `api.tavily.com` and
  `integrate.api.nvidia.com` succeed through the SAME node — the WARP→Fastly
  IPv6 leg is the degraded piece, and `check_warpgo_fleet.sh` probes only
  `www.cloudflare.com`, so its 3-strike restarts (wg-port-1090.last-restart =
  22:45) cannot see it. Don't reconfigure the fleet as part of a harvester
  change (googleaisearch2api shares those ports).
- The edge IP pool (`github_transport.edge_pool`, `hosts.ohmygh.com` + SNI) stays
  disabled whenever a proxy is set unless `prefer_over_proxy: true`
  (`search/github/transport.py`); it is the only built-in proxy-free path for
  `api.github.com` and is still UNPROBED — measure before recommending it.

**Deployed 2026-09-24 23:41 CST (`4f6a66e`)**: fnos aligned (rollback
`rollback-20260924-233832`, 0 local-only commits, `Dockerfile.web` restored +
`docker-compose.hostnet.yml` re-applied), whole tree `docker compose cp`-ed,
`docker compose restart` (no recreate → env and `/tmp` kept), `/health` ok,
`search/client.py` md5 host↔container MATCH.

**Salvage before the restart** (pushes only fire on completion, and each
provider's next cron resets `valid-keys.txt` with `auto_restore: false`): the
15 stuck runs held **1,193 validated keys**, all pushed through the official
services — nvidia 102→+16, tavily 398→+48, serpapi 462→+19, openrouter
81→+7 (agnes-ai 30 / modelscope 17 / qwen-cn 3 were already pooled) = **90
net-new keys**; command shape is in the egress skill. Startup reconciliation
then flipped the 15 rows to `failed: interrupted by service restart` (0 rows
left `running`, guards cleared for the 01:00-17:00 chain).

**Functional proof on production** (deployed module, single live run
`kimi-coding`): `effective_use_proxy(raw)=False`, `effective_timeout(raw,10)=20.0`,
`http_get(raw_url, timeout=10)` → 29,583 B in 0.98 s; a scratch failover demo
rotated `1080 → 1090` after 3 consecutive transport failures with the masked
WARNING line. Rate comparison over one 40-min stdout window split at the
restart (pre = 15 stalled runs, post = 1 fixed run): `SOCKSHTTPSConnectionPool`
14,147 → **0**; `[gather] error` 4,498 → 2; `[search] error` 466 → 0;
`search completed` 16 → 84; log lines 38,149 → 1,002. The post-window raw
fetches show up as `HTTPSConnectionPool … read timeout=20.0` (direct + floored),
and kimi-coding's `links.txt` went 465 → 3,500+ in ~12 min.

**Log-probe trap (cost the first metric read, verified at fd level)**: any
in-container `python` invocation re-initialises the logger, and
`constant/system.py:23 DEFAULT_LOG_CLEANUP_DELETE = True` (a compile-time
constant, not env-toggleable) makes that initialisation **DELETE** every
`logs/<module>.log` it has not seen yet AND `_delete_existing_logs()` wipes
`main.log`. The previously-running app keeps writing to the now **unlinked
inodes** (`ls -l /proc/1/fd | grep -i log` → `… log (deleted)`), so:

- an empty or stale `/app/logs` after an in-container python probe means the
  sink was destroyed — **NEVER** "no activity"; every recipe below that greps
  `/app/logs` (manager.log `Recovered N unique links`, the zero-yield ERROR
  line, retry.log failure mix) reads empty until the next process start;
- the correct probe shape is `docker compose exec -T harvester-web sh -c 'grep …'`
  or host-side python; `docker compose logs --since … | grep` on the HOST is the
  only sink that survives a probe (stdout carries every record);
- dead inodes keep consuming disk invisibly to `ls`, so a probe-heavy day is
  also a disk-leak day.

**The sink partially self-heals — do NOT restart just for it**:
`SafeRotatingFileHandler` reopens the path on the next rollover and
high-volume handlers reopen on emit, so `main.log` / `stage.log` / `state.log` /
`web.push.log` come back on their own within one rotation (verified 09-26:
`main.log` rolled at 09:14 and keeps carrying every record; only low-volume
modules — storage/tools/web.app/web.runner/web.scheduler — keep deleted fds
until the next process start). A restart to "restore file logging" kills every
live scan and forces another salvage round, for a cosmetic gain.

**Verification after the 2026-09-25→26 chain (log rotation chain 09-25 15:39 → 09-26 09:35, ≈18 h)**:
`raw.githubusercontent.com` SOCKS failures **0** (was 325k/28 h); the only
residual SOCKS failures are on proxied legs — `api.github.com` 1,092 (~84/h,
was ~1,200-1,400/h), `integrate.api.nvidia.com` 355, `api.tavily.com` 1;
`Proxy failover` fired 508 times (152/152/149 in each direction — all three
exits have bad patches, the rotation keeps traffic moving); direct raw fetches
log 21,068 `read timeout=20.0` retries (~2.7 % of ~770k link fetches, retried);
`[gather] error` fell ~27x; `search completed` 13,328 in ≈18 h (~740/h, was 2-20/h cluster-wide);
zero-yield tripwire lines 0. Runs now complete end-to-end with real corpora
(openrouter 76 min / 18.8k links / 59 valid, agnes 71 min, modelscope 306 min,
ollama 180 min, serpapi 269 min / 240 valid, tavily 23.4 h / 146.7k links / 505
valid, deepseek 28.3 h / 336k links), and `push_logs` added **+70 net-new keys**
since 09-25 (tavily +30, serpapi +25, openrouter +8 …).

**Residuals this verification exposed (recorded, not fixed)**:
1. `request()` counts ANY HTTP status as "the exit works" before
   `raise_for_status`, so an exit that answers 403/429-to-everything resets the
   failover streak and can never be rotated away — diagnosing that flavour
   again needs the per-node `SOCKS5 CONNECT` census, because the transport
   streak counter will read zero. **Guard added 2026-09-26 (`18b1d84`)**:
   `_note_proxy_status()` counts the consecutive *degraded* (403/429) streak,
   warns once at `>= _PROXY_DEGRADED_WARN_THRESHOLD` (25) and exposes
   `get_egress_state()['degraded_streak']`; rotation is deliberately unchanged
   (a 403/429 from `api.github.com` is usually token/quota state, not exit
   state), and the streak only counts answers WITHOUT rate-limit headers
   (`_has_rate_limit_headers`: api.github.com advertises `x-ratelimit-*` even
   on 403, tavily sends `retry-after`) — quota-level answers reset it, so the
   warning does not cry wolf during routine secondary-rate-limit storms.
2. **Silent no-op runs — root-caused and guarded 2026-09-26 (`18b1d84`)**.
   Mechanism VERIFIED from logs+code (the earlier "credential cooldown" reading
   was wrong — `tools/credential.py::_get_available` *waits*, it cannot hand the
   stage a None token): the 08:00 openrouter run had **ONE condition**, so one
   SearchTask was the whole run; the task was denied by the **process-wide
   `github_api` limiter** (`client.py` "Rate limiter denied request for
   github_api (no token after wait)"; 7 overlapping scans share that bucket) on
   the task's attempts, with the REAL budget: denials at 08:00:32 → requeue
   (1 s), 08:00:43 → requeue (4 s), 08:00:50 → `should_retry(attempt=2, e)`
   reached `max_retries=2` and returned False, i.e. **3 attempts / 2 requeues**
   — and the terminal drop produced **no log line at all** (`put_task`'s
   "discarded, max retries" warning covers only the dedup path; `grep -c` = 0
   in the run's window and `stage/base.py:247` was never reached). The run then
   "completed" 54.5 s after start with links=0 / materials=0 / `error_message`
   NULL. Three guards:
   * `stage/base.py::_worker_loop` logs the terminal drop at WARNING
     (`task dropped after N attempt(s), retry budget exhausted: …`);
   * `GitHubClient._limit` waits until a **total deadline**
     (`_LIMIT_WAIT_DEADLINE_SECONDS` = 15 s, per-round floor 0.5 s, 64-round
     runaway guard) before denying — the earlier round-capped sketch was a
     no-op in the very token-steal race it targeted (`wait_time()` reads ~0
     while a sibling thread holds the refilled token, so 6 rounds burned in
     0.6 s). The 15 s cap keeps a stop request from being held hostage much
     beyond `BasePipelineStage.stop`'s per-worker join budget (30 s split
     across workers); three attempts still give ~45 s of patience per task.
     The denial is `core.exceptions.RateLimiterDeniedError` (subclasses
     ConnectionError so every retry predicate keeps working) and the three
     stage-log sites (`definition.py:257`, `base.py:389/463`) drop to WARNING
     by TYPE, not by message match (they were a large share of the 50 MB/6 h
     volume);
   * `web/runner.py::_no_work_degradation` records a
     `no-work run: …` marker in `run_records.error_message` for completed runs
     with links=0 / materials=0 / valid_keys=0, quoting the run's own stage
     counters (`_stage_error_snapshot` → e.g. `search_completed=0
     search_failed=3`); the zero-yield tripwire needs >1000 links and cannot
     see this class.
3. Search now works, so per-run corpora ballooned to 80k-146k links
   (deepseek 336k) and heavy runs take 14-28 h; they then collide with the next
   day's chain — `already running — skipping` measured for github (09-25 12:50,
   09-26 00:50, 06:50) and deepseek (09-26 02:00). The GitHub code-search quota
   (10 req/min per token × 7 tokens, secondary-limit cooldowns on top) is the
   new cluster-wide ceiling.

**Framing after the fix (2026-09-26)**: with search working, per-run corpora
ballooned (openrouter 18.8k, modelscope 35.4k, glm 119k, tavily 146.7k,
deepseek 336k links) and 14-28 h runtimes are *legitimate work*, not stalls —
22 runs completed 09-25→26 with real yields (tavily 505, serpapi 240,
openrouter 59, modelscope 59, ollama 40, agnes-ai 38 valid keys; glm / glm-ai /
kimi-ai 0 valid is EXPECTED under the flash-only policy, not a regression).
`run_records.started_at` is UTC while cron/logs are CST (+8): the 09-26 morning
"7 running rows" were nvidia 11:50 (09-25, ~22 h), github 18:50 (~15 h),
groq 09-26 01:00, kimi 04:00, mimo-cn 06:00, qwen-cn 07:00, tavily 09:00 —
all progressing (links/material deltas measured), none stalled. The binding
constraint is corpus volume × concurrency: overlapping runs collide with the
next chain tick (`already running — skipping`: github 09-25 12:50, 09-26 00:50,
06:50; deepseek 09-26 02:00), so the durable lever is bounding work per run
(`max_pages` / per-run link caps) rather than more egress work. Watch the
documented pattern tripwires too: kimi-coding spent 16.6 h for 73 materials /
1 valid from 84,635 links — that is the "near-zero candidates at the check
stage" signal — 2026-09-26: this was re-measured and REFUTED for kimi-coding
(0 old-pattern hits, 4 wide-only hits that were all placeholders; the run's own
check outputs were entirely `sk-kimi-`-shaped), so the pattern stays narrowed
and the real lever is dork/class diversity. The GitHub code-search budget (7 tokens × 10 req/min + secondary
rate-limit cooldowns; 16,919 all-tokens-cooling pauses in ≈18 h) is the
search-leg ceiling and cannot grow while the `github` self-bootstrap run
produces no new tokens.

**Deployed again 2026-09-26 10:47 CST (`691268f`)** — review fixes on top of
`18b1d84`: deadline-driven `_limit` wait (the round cap was a no-op in the
token-steal race), typed `RateLimiterDeniedError` for the stage-level WARNING
downgrade (no message matching), rate-limit-header gating on the degraded
streak, and stage counters in the no-work marker. Salvage first (openrouter 46
keys → already pooled), fnos aligned (rollback `rollback-20260926-1047xx`),
md5 MATCH for `core/exceptions.py` / `search/client.py` / `stage/base.py` /
`web/runner.py`, markers present, `/health` ok, 0 rows left `running`. Clean
worktree at `691268f`: **820 tests OK / 8 skipped**.

**Deployed 2026-09-26 10:23 CST (`18b1d84`)**: salvage first (the 7 live runs'
files: nvidia 1,057 keys → already pooled, agnes-ai 54 → +30, tavily 285 → +3;
github/groq/kimi/mimo-cn/qwen-cn had no keys), fnos aligned (rollback
`rollback-20260926-1022xx`, 0 local-only commits), whole-tree cp, restart;
md5 host↔container MATCH, all three marker groups present
(`client.py` 4 / `base.py` 1 / `runner.py` 2), `/health` ok, the 7 `running`
rows reconciled to `failed: interrupted by service restart` (0 running).
Functional check: openrouter re-triggered manually — see its row for the
post-deploy behaviour.

**kimi-coding pattern tripwire RESOLVED — do NOT widen (measured 2026-09-26)**:
the tripwire fired on the 09-25 run (84,635 links → 73 materials / 1 valid), so
the pattern was re-measured against the same-generation corpus: a deterministic
339-body sample (blob→raw mapped, fetched directly) matched **0 bodies with
`sk-kimi-[0-9A-Za-z_-]{20,}`** and 4 with the wide `sk-(?!ant|proj|svcacct-)
[A-Za-z0-9]{16,}` — and all 4 wide-only hits are placeholder/fixture strings
(`sk-xxxxxxxxxxxxxxxx`, `sk-liveSECRET1234567890abcdef`), i.e. exactly the noise
class the 2026-09-21 narrowing removed. The tripwire run's own check outputs
(`backup-20260926-190000/`): 80 material rows all `sk-kimi-`-shaped (~50
distinct), 12 wait-check all authentic `sk-kimi-`+72, 36 invalid, **zero
wide-only candidates reached check**; the wide era's own probes (backup
`20260920-040003`) had 463 candidates → 0 valid with all 13 plan-gated
authentic keys `sk-kimi-`-shaped. Verdict: starvation is CORPUS COMPOSITION
(≲0.1 % of links on this dork set carry any key-shaped string), not extraction —
if a future run again shows near-zero candidates, widen the measurement once,
but then chase dork/class diversity, not the pattern.

**Admission knobs are wired into both compose files (2026-09-26)**:
`HARVESTER_MAX_CONCURRENT_SCANS` / `HARVESTER_DEFER_DELAY_SECONDS` /
`HARVESTER_MAX_DEFERRALS` are now passed through in `docker-compose.yml` and
`docker-compose.hostnet.yml` (defaults match the code: 6 / 1800 / 8) — before
this, "env-overridable" was fiction on prod because both files use an explicit
`environment:` list with no `env_file`. They take effect on a **recreate**, not
a `restart` (container env is frozen at creation).

**Functional proof of the cap (bounded CLI run, 2026-09-26 22:2x CST)**: a
scratch config in the container's `/tmp/cap-probe` (one dork, gather/check/
inspect off, `max_links_per_run: 3`, workspace + cwd isolated there so the
logger's CWD-relative `logs/` and the shared `data/queue_state` were untouched;
token passed via `GITHUB_TOKENS` env, never written to a file; hard-bounded with
`timeout -s KILL 300` because main.py swallows SIGTERM). Evidence:
`[search] per-run link cap reached (3 links queued for gather) for openrouter —
dropping further search tasks` (once), `links.txt` = 100 (exactly one page),
`[search] search completed … 100 links, 19 keys, total: 73728` — i.e. the
73,728-result tail was never walked, run finished in 80 s. Note the probe ran
the build deployed at 20:44, which still generated the page/refine tail for the
crossing task (27 refined + 9 page tasks, all dropped at entry); the mid-task
short-circuit added in the next commit removes that churn.

**Evaluation baseline (the "before" set for the 2026-09-26 20:44 deploy)** —
`run_records` since 09-25 12:00 UTC + `push_logs.added_count` per provider:

| provider | duration | links | valid | push added |
|---|---|---|---|---|
| serpapi | 269 min | 33,265 | 240 | 25 |
| ollama | 180 min | 15,217 | 40 | 1 |
| modelscope | 61 min | 35,687 | 59 | 0 |
| tavily | 1,402 min | 146,704 | 505 | 33 |
| glm | 1,181 min | 119,040 | 0 | 483 |
| glm-ai | 974 min | 86,863 | 0 | 63 |
| nvidia | — (killed live, 8.9 h) | — | 965 in file | 657 |

Compare after the cap lands with **`push_logs.added_count` + `links_total` +
`duration_seconds`**, never `valid_keys_found` alone (a capped corpus shrinks
NEW discovery while re-found duplicates keep re-validating — the ollama lesson).

**Edge-pool idea REJECTED for the web path; UNPROBED for a proxy-free run
(measured 2026-09-26)**: do not enable
`github_transport.edge_pool.prefer_over_proxy` in web mode — the runner always
injects `HARVESTER_PROXY` into the runtime YAML, and with a socks5 proxy set the
adapter's IP rewrite is never consulted (it monkeypatches
`urllib3.util.connection.create_connection`, while PySocks dials through
`SOCKSConnection`), so the flag would only log "[edge] using N … IPs" while
every request still traverses SOCKS. Two separate facts behind that: Two independent reasons: (1) the feed it depends on,
`https://hosts.ohmygh.com/v1/hosts`, is dead — `curl: (6) Could not resolve
host` from the fnos host, and Cloudflare 530/502 from other vantages (the same
530 is already in the 2026-09-22 local run log); (2) structural: the edge
adapter rewrites the dial target by monkeypatching
`urllib3.util.connection.create_connection`
(`search/github/adapter.py`), which PySocks bypasses whenever a socks5 proxy is
configured — so with a proxy the edge IP is never used, and without one the pool
falls back to UNVERIFIED cached IPs and never rotates back to the proxy
(`transport.py` cooldown/next_ip). Note what is NOT disproven: the feed being
dead only blocks pool *refresh* — `init_edge_pool(background=True)` uses the
cache immediately — and the prod cache still holds 8 verified IPs from
2026-08-12, so a CLI run with the proxy env unset could still exercise a
direct+seeded edge path. Nobody has probed those cached IPs against
`api.github.com` yet; it stays a candidate for a proxy-free CLI experiment, not
a settled question either way. Also: the container image is python:3.12-slim
— there is NO `curl` inside it; in-container probes must be python.

**Admission control + corpus bounding (deployed 2026-09-26 20:44 CST, `22561f4`)**: three
guards against the pile-up class, all configurable:
* `global.max_links_per_run` (`config/schemas.py`, default **120000**, 0 =
  unlimited): `SearchStage` tracks links **queued for gather** (post
  link-index filtering, i.e. the fetches that drive runtime — not raw
  discoveries, and not `run_records.links_total`, which counts persisted
  links) and, once the cap is reached, (a) drops further search tasks at worker
  entry with one WARNING and (b) skips pagination/refinement generation for the
  task that crossed it. The bound is therefore on the *tail*: a single page can
  overshoot by ≤100 links. Duration effect is PROVIDER-DEPENDENT — measured
  drain rates spread ~7x (modelscope 582 links/min → 120k ≈ 3.4 h; serpapi 124
  → ≈16 h; tavily 105 → ≈19 h; ollama 84 → ≈24 h), and the slow ones are
  check-bound rather than gather-bound, so links are not their real knob. Treat
  120k as a runaway-corpus guard, not a duration SLA; a per-provider override
  is a follow-up. The biggest corpora still collide with the next cron tick for
  those providers — stated as a known limit, not fixed by the deferral ladder.
* `web/scheduler.py`: `_MAX_CONCURRENT_SCANS` (env
  `HARVESTER_MAX_CONCURRENT_SCANS`, default 6, 0 = unlimited) counted from
  `run_records` (`_active_run_count`, which FAILS OPEN when the count cannot be
  read); a firing that hits 409 (provider live) or 429 (cap) is now **deferred**
  instead of being dropped — github's 6-hourly cron used to vanish silently
  whenever its daily run was still live. Details that matter:
  * the ladder **escalates**: `_DEFER_DELAY_SECONDS * (attempt + 1)`
    (env `HARVESTER_DEFER_DELAY_SECONDS`, default 1800 → 30, 60, 90 … min),
    `_MAX_DEFERRALS` 8 (env `HARVESTER_MAX_DEFERRALS`) ≈ 18 h of coverage;
  * **one ladder per provider**: `has_pending_deferral()` is queried from
    APScheduler (`defer-<provider>-` job ids), not from a parallel flag — a
    one-shot job disappears by itself, so the check cannot desync into refusing
    every future deferral;
  * the deferral's `run_date` is built in the **scheduler's** timezone
    (`datetime.now(scheduler.timezone)`): a naive date is interpreted in that
    zone by APScheduler, and the dev box's C-library local zone differs from
    Asia/Shanghai by 7 h (prod: container UTC vs fnos-host CST);
  * a firing folded into an existing ladder logs WARNING ("folded into its
    pending deferral ladder"); only a budget-exhausted drop logs ERROR ("firing
    DROPPED") — the line a log-based accounting should count;
  * `init_scheduler` sets `job_defaults={coalesce: True, max_instances: 1,
    misfire_grace_time: None}`. APScheduler's default grace is ONE SECOND, so a
    firing landing >1 s late (load ~8 box) was silently skipped; any FINITE
    grace just moves that cliff, hence None = never expire — a late firing runs
    and then lands in the cap/guard→defer path, which is what makes a busy box
    safe. Deferral jobs inherit the same `misfire_grace_time=None`.
* `web/runner.py::_no_work_degradation` now returns `(message, failed)`: a run
  that searched and produced nothing (links=0/materials=0/valid=0 AND ≥1 search
  attempt) is recorded **failed** (visible + re-triggerable) instead of
  `completed` with a note; a validate-only config keeps `completed`.

**Log-sink nuance (measured 2026-09-26)**: `web.runner` lifecycle records DO
reach the container stdout sink (4 h window: 7 `Scan started`, 2
`Scan completed`, 23 `[runner.py` lines) — an earlier 0-count window was simply
2 hours in which nothing started or finished. So: stdout is valid for
throughput/error-rate metrics AND for lifecycle, but `run_records` is the
authoritative lifecycle source (it also covers restarts that kill a run before
its terminal write).

**Deployed 2026-09-26 20:44 CST (`22561f4`)** — the three guards above.
Salvage first (7 live runs: nvidia 965 keys → +12 new, glm 375 → +12, glm-ai 46
→ +15; github/kimi-ai/kimi-coding/mimo-sg had none), fnos aligned (rollback
`rollback-20260926-2044xx`), whole-tree cp + restart, md5 MATCH for
`config/schemas.py` / `config/loader.py` / `stage/definition.py` /
`web/scheduler.py` / `web/runner.py`, markers present, `/health` ok, 0 rows left
`running`. Clean-worktree suite at the deployed SHA: **837 OK / 8 skipped**.

**Live proof of the admission cap (measured, 20:46 CST)**: six runs were
triggered manually (deepseek, kimi, mimo-cn, github, qwen-cn, serpapi → 202
each), `run_records` showed exactly 6 `running`, and the seventh trigger
(kimi-ai) returned **HTTP 429 `concurrency cap reached (6/6 runs live) —
deferring instead of stacking another scan`** — i.e. the DB-counted check works
against the real schema (no fail-open) and a refused start does not leak the
scheduler guard. The cron-side deferral path (`_run_provider_job` → one-shot
DateTrigger) has its own observation point at the next firings (00:00-00:50 CST
while these six are still live), plus its unit tests.

**Deployed again 2026-09-26 23:0x CST (`455ceb5`)** — the round-2 review fixes
(escalating 30×n ladder / `_MAX_DEFERRALS` 8, APScheduler-queried single ladder
with EXACT provider-id matching, `job_defaults` incl. `misfire_grace_time=None`,
tz-aware deferral dates, gather-based cap accounting + mid-task short-circuit,
compose env plumbing). Salvage first (serpapi 107 keys → 0 new; deepseek / kimi
/ mimo-cn / github / qwen-cn had none), fnos aligned (rollback
`rollback-20260926-23xxxx`), whole-tree cp + restart, md5 MATCH for
`web/scheduler.py` / `stage/definition.py`, markers present, `/health` ok, 0
rows left `running`. Clean-worktree gate at the deployed SHA: **843 OK / 8
skipped** (886 OK / 8 skipped in the dirty tree).

**LIVE proof of the deferral path (deployed build, 21:00-21:30 CST)**: while six
scans were live, `qwen-intl`'s 21:00 cron firing hit the cap and was **deferred
three times, then dropped** —
`Provider qwen-intl deferred (attempt 1/3, retry in 900s): concurrency cap
reached (6/6 runs live)` at 21:00, the same at 21:15 (attempt 2/3) and 21:30
(attempt 3/3) — i.e. the APScheduler 429→cap→DateTrigger chain works in
production, and the SAME run also showed why the ladder had to grow: a 3×15 min
budget expires inside the collision (the round-2 build now covers ≈18 h).

**Still to be proven by real runs (do not read as done)**: (a) the round-2
ladder (8 attempts) and its `folded into its pending deferral ladder` WARNING —
next natural collision; (b) the 120k cap firing in a production run (`per-run
link cap reached` was 0 as of 23:05 — the six 20:49 runs had not crossed it);
(c) the duration effect of the cap (proved only by the bounded CLI run so far);
(d) the `no-work run → failed` status on a real starving run (no occurrence
since the deploy). The comparison baseline for (c)/(d) is the table above plus
`push_logs.added_count` per provider.
