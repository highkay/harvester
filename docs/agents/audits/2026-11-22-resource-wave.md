# Audit 2026-11-22 — resource wave: durable cap=1, deferral wake, queue-state skip, chain-blocker bounds, evict retirement

> Triggered by the operator's own mitigation: on 2026-11-20 he capped
> concurrency at 1 for the fnos box — via an untracked
> `docker-compose.override.yml` (created 15:19:25, auto-merged by compose,
> also carrying `init: true` + the healthcheck) plus a shell env var at the
> recreate — then asked for a root-cause re-audit ("目前项目有资源占用的问题，
> 我在线上进行了缓解"). The audit (same morning) measured the four causes
> below; a bare `ulw` approved landing all of them. Evidence lives in
> `.omo/evidence/resource-wave-20261122/` and the gate review at
> `.omo/evidence/resource-wave-20261122-gate-review.md`.
>
> **Correction (recorded same day, during the doc-alignment pass): the
> original audit claim that the cap "lived only in the frozen container
> env and would silently revert to 3" was wrong** — the override file had
> already pinned it durably and was missed because it is untracked. The
> value is now pinned in three agreeing places (override file, fnos `.env`,
> hostnet compose default); what the wave actually added on this axis is
> the committed-and-reviewed repo default plus the stale-comment fix, not
> rescue from a silent revert. The override also owns `init: true` and the
> container healthcheck, so it stays.

## What the morning audit measured (all prod, 2026-11-22)

- The cap=1 injection lived only in the frozen container env: `.env` and both
  compose files still defaulted to 3, so any recreate without the shell
  variable silently restored 3-way scan overlap on the 4-core fnos box.
- The deferral ladder retries on a fixed linear grid (30 min × attempt, 8
  attempts, worst 18 h, then DROPPED) and never observes slot release:
  clinepass held the single slot 23:00→06:05 while nvidia's ladder (armed
  02:30) was still waiting past 08:00 after a fresh deepseek cron fire took
  the free slot at 08:00. Deferred lines 11-20/21/22: 2/5/13.
- `manager/queue.py` rewrote the full in-flight queue as indented JSON every
  60 s (`persistence.queue_interval`): **14.1M task serializations / 41 h**
  (gather 11.2M, check 2.6M) ≈ 10 GB — ~25% of the container's 46.4 GB block
  writes — while `auto_restore` defaults false, so no clean-start run ever
  read the files back. Only reader: the `auto_restore` branch in
  `manager/task.py::_on_start`.
- clinepass (5 runs, 0 valid keys, 6.2-7.3 h each) and github (16 runs,
  3 valid total, 3.8 h search-bound: 2,093 pages × 0.15 req/s) monopolized
  the serialized slot; the tavily evict loop had idled 7,556 zero-work
  rounds for 55 days (designed 130 rounds; `CYCLES=10080`, re-armed by a
  systemd user timer).

## Landed commits (origin/main `cde61c5` + nit fix `304e790`)

| Commit | Change |
|---|---|
| `eb6dd48` fix(web) | `_watch_run`'s finally calls `_wake_pending_deferrals`: the OLDEST pending `defer-<provider>-<ms>` job is removed and re-dispatched through `_run_provider_job` with its stored attempt count the moment a slot frees. Sync block (no await) so two releases cannot wake the same ladder; `_shutdown` flag set before watcher cancellation; excepts narrowed to `SchedulerNotRunningError`/`JobLookupError`. |
| `fd55cad` fix(manager) | `start_periodic_save` only when the profile has `auto_restore` — clean-start runs stop paying the 60 s full-queue rewrite. Stop-path saves (pipeline stop, GracefulShutdown) remain, so a SIGTERM'd run is still replayable by a resume profile; only hard-crash in-flight replay is given up. |
| `d7496e0` fix(examples) | clinepass `max_links_per_run` 9000→2000 (whole GitHub corpus ~11.4k files, so a weekly 2000-link sample still sweeps it); github gains 30000 (guards the gather/check tail; search time is bounded by the dork set — a dork diet is a separate ROI decision). Weekly floor kept, nothing disabled. |
| `cde61c5` fix(build) | `docker-compose.hostnet.yml` (the fnos deploy source) defaults `HARVESTER_MAX_CONCURRENT_SCANS` to 1; dev bridge compose keeps the code default 3; stale MAX_DEFERRALS=6 comment corrected (prod runs 8). |

Tests: `TestDeferralWake` ×5 (admission file), autorestore owner tests extended
with `start_periodic_save` called/not-called, `TestChainBlockerCorpusBounds` ×2
(pins 2000/30000). Full suite **1767 OK / skipped=12**; ruff delta zero on
changed files; pyright 14→14 pre-existing. Both new gates mutation-verified
red. Cap=1 is additionally recorded in fnos `.env` (backup
`.env.bak-20261122`) — belt and suspenders with the compose default.

## Deploy and verification

Zero-kill recreate 08:54 CST (qwen-cn gap): `printenv` cap=1 **without any
shell variable**, `/health` ok, startup recovery re-pushed agnes-ai's 13
stranded keys, catch-up re-armed nvidia (09:09) and tavily (ladder 09:24).
The tavily evict watchdog timer is disabled+inactive, its `/tmp` script,
unit files and staging removed; the bind-mount logs stay frozen as history.

Post-deploy cycle (12.4 h, 6 runs, zero DROPPED, zero idle slot between
chained runs):

| Run | Window (CST) | Duration | Valid | Push | Handoff |
|---|---|---|---|---|---|
| nvidia | 09:09→12:25 | 3.27 h | 833 | success 5+828 | catch-up |
| tavily | 12:25→14:22 | 1.94 h | 342 | success | **wake 3.9 s** |
| serpapi | 14:22→15:53 | 1.52 h | 258 | success | wake |
| agnes-ai | 15:53→16:07 | 14 min | 15 | failed (pre-existing) | wake |
| qwen-intl | 16:07→16:27 | 20 min | 0 | — | wake (16:00 cron hit cap, grid would be 16:30) |
| mistral | 20:00→21:04 | 1.08 h | 1 | success +1 | slot free |

- 4 wake pairs total; chained handoffs 4–8 s (one watcher poll). The
  qwen-intl case is the direct proof that a fresh cron fire no longer jumps
  ahead of an older pending ladder.
- queue_state: **0 periodic save lines across 12.4 h / 6 runs**; the four
  file mtimes only move at each run's terminal write (stop-path).
- Evict logs frozen from 08:51 through the next day.
- Aggregate BlockIO 13.9 GB / 12.4 h ≈ old average — the day contained the
  190k-link nvidia run, so cross-day aggregate rates are run-mix dependent;
  the queue-state share being structurally zero is proven at the mechanism
  level (mtimes + zero save lines), not by the aggregate.
- Gate review (omo-native-gate-reviewer): **APPROVE / HIGH**, all seven
  criteria independently re-verified in repo and prod; notes fixed in
  `304e790` (CRLF comment was wrong for the LF-committed blob; the wake
  arity guard now actually covered by a fixture case).

## Leftovers (separate workstreams)

- **agnes-ai push failing** since 2026-11-21 (both days `push_logs` failed,
  added=0): the generic push logs "No gpt-load mapping for provider
  'agnes-ai'" and the satellite push is locked out by its already-pushed
  idempotency marker, so ~15–22 keys/day do not land. Pre-existing, not
  introduced by this wave. Memory:
  `reference/harvester-agnes-push-failure-20261122.md`.
- clinepass/github run-duration comparison after the bounds: first natural
  runs are Saturday 2026-11-28 (`0 23 * * sat` / `0 15 * * sat`); expected
  clinepass ~7 h → ~3 h.
- A dork diet for github (search-bound at 3.8 h) belongs to the next ROI
  round with the operator, not to a code wave.
- The dev bridge compose stays at cap 3; only the fnos hostnet compose pins 1.

## Traps recorded

- `nohup` from a foreground bash call gets reaped by the harness — the full
  suite must run through a persistent background bash session; a throttled
  suite shows stale buffered log timestamps and looks hung while dead.
- Three-level quoting (ssh → docker exec → python -c) breaks: use
  `ssh admin@fnos "docker exec -i harvester-web python -" < local.py`.
- Waiting discipline: write the ETA as an absolute clock time and arm TWO
  channels (streaming `docker logs -f` + a 60 s poll gate) — the ssh stream
  can drop silently.
