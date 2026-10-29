# audit 2026-09-30 (false-404 duo + lifecycle/elasticity) - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Ops: 2026-09-30 audit → root-cause fix sprint (false-404 duo + lifecycle/elasticity)

A full-loop audit (4 parallel evidence agents against prod + 4 diff-review
agents over the 09-20..09-28 fix chains) plus a fix sprint. Evidence:
`.omo/evidence/ulw-20260930-prod-audit/` and `.omo/evidence/ulw-20260930-rootfix/`.
Ten commits on main (`9bc65c7`..`0235c42`), all TDD with RED→GREEN captured;
suite 932 → **984 OK / 8 skipped**.

- **The 404 "noise" was a yield bug, refuting the earlier audit reading.**
  3484 `File not found` gather errors/48h = 65% of all ERROR lines, previously
  filed as "dead tail links, expected noise". Measured decomposition: **96.3%
  are FALSE 404s from our own URL mangling** (the files exist — live re-fetch
  200, 30/30 host + 10/10 container), ~2.2% a second false-404 mechanism, only
  ~1.5% genuinely dead (0.004% of all fetches). Downgrading the log level
  would have MASKED a real yield loss — do not do it; the classes are fixed
  at the mechanism instead:
  - **F1 `tools/utils.py::encoding_url` punycode bug (96.3%)**: it applied
    `xn--` punycode to CJK characters in the URL *path* (IDNA is only valid
    for hostnames) — prod wire form `code/xn--wbs215fqga/4.6.ipynb` → 404,
    correct form `code/%E7%AC%AC%E5%9B%9B%E7%AB%A0/4.6.ipynb` → 200. Now:
    IDNA label-wise on the netloc only; path/query/fragment percent-encoded
    via `requests.utils.requote_uri` (existing valid escapes preserved, raw
    space → `%20`, idempotent, pure-ASCII byte-identical, never raises).
    The two 404 log sites in `search/client.py` now print `url:` AND `wire:`
    so a fabricated URL can never hide again. Tests: `tests/test_url_encoding.py`.
  - **F2 `github_blob_to_raw` `#` truncation (~2.2%)**: `urlsplit` treated a
    `#` that is real PATH content (dir `C#Code/`, AutoSDK `_#G.Models…`
    filenames; 367 links.txt lines carry `#`) as a fragment → truncated
    (`…/C#Code/…` → `…/C` → 404; proven: `C%23Code` form → 200). Now parsed
    structurally: only a trailing line anchor `#L\d+(-L\d+)?$` is stripped,
    every other `#` → `%23`, pre-encoded `%23` never becomes `%2523`,
    query/non-github/malformed inputs stay byte-identical. Chain pinned by
    `TestBlobToRawToWireChain`.
- **F3 degraded-exit rotation** (`e97f1b8`): a proxy exit answering 403/429 to
  EVERYTHING used to be warn-only (proven live 09-29: 30 warnings over 2.5h
  during an nvidia run). At a 25-streak with ≥2 `HARVESTER_PROXY` candidates
  it now rotates via the same `_apply_proxy` mechanics as transport failover,
  re-arming per streak; the rate-limit-header gate is unchanged — a
  quota-flavoured 403/429 (x-ratelimit-* / retry-after) still resets the
  streak and can NEVER rotate. `get_egress_state()` gains
  `degraded_rotations`.
- **F4 catch-up duplicate-fire** (`068fc88`): `_run_catchup_job` re-checks at
  EXECUTION time (shared `_catchup_state` helper with the arming path) and
  skips when a run ≥ the missed fire exists or a live run is present — before,
  a cron firing during the catch-up stagger delay produced a 409 → deferral →
  duplicate run ~30 min later. Re-check failure fails toward running
  (WARNING + proceed), pinned by test.
- **F5 no-work predicate** (`38a9e3f`): a zero-output run is now `failed` ONLY
  when every search attempt died (`search_failed>0 AND search_completed==0`);
  a genuinely-empty successful search stays `completed` (the old
  `search_attempts>0` predicate misclassified it).
- **F6 limiter idempotency** (`ecf7d99`): `init_github_client` no longer
  rebuilds the process-wide GitHubClient/RateLimiter on every scan start —
  it early-returns when the limits mapping compares equal (per-scan rebuilds
  were resetting the shared token buckets under in-flight scans). Rebuild on
  changed limits, lock-guarded, INFO log only on real builds.
- **F7 watch deadline** (`8f4a573`): `_watch_run` was unbounded — a scan
  thread whose terminal DB write fails left the row `running` forever,
  wedging that provider's cron + inflating `_active_run_count` until restart.
  The watcher now expires at `started_at + HARVESTER_RUN_WATCH_MAX_HOURS`
  (default 36h > longest measured 28.3h run; falls back to watch-start),
  releases the guard, logs ERROR (short run id), and best-effort flips the
  row `running→failed` via `_update_run_sync(only_if_running=True)`.
- **F8 push recovery retries** (`0235c42`): `find_unpushed_terminal_runs` now
  suppresses a run only when a `status='success'` push_logs row exists —
  failed/partial pushes are re-dispatched at startup (duplicate-safe: targets
  dedup server-side). Before, ANY push_logs row (even failed) made a run
  terminal-invisible to recovery.
- **F9 opencode 401 taxonomy** (`8f06bea`): only the two measured dead-key
  shapes map to INVALID_KEY (wrapped `AuthError`; no-wrapper `server_error`
  "Invalid credential"); unparseable/unrecognised 401 bodies → UNKNOWN.
  NOTE: UNKNOWN still routes to `invalid-keys.txt` by the deliberate 09-23
  house rule ("verdict genuinely unknowable → invalid") — F9 improves
  diagnostic precision, NOT recoverability. If you want unproven 401s to be
  recoverable, the routing policy is the thing to change (house-level
  decision, deliberately not churned here).
- **fnos watchdog fixed (root cause verified live)**: the tavily eviction loop
  had been dead since 09-29 15:10 — the systemd user service is
  `Type=oneshot` + default `KillMode=control-group`, so systemd SIGTERMed the
  watchdog's `nohup`d loop child the instant the script returned (it never
  even wrote its banner). Fix: `KillMode=process` in
  `~/.config/systemd/user/tavily-evict-watchdog.service` (backup
  `.bak-20260930-113313`), daemon-reload, manual start — the loop survived the
  next timer tick (11:48) and cycles normally. Tripwire for any future
  systemd-user "fire and forget" pattern: oneshot + control-group kills
  backgrounded children; use KillMode=process or a Type=simple service.
- **fnos docker `--build` root cause identified**: the dependency is the
  `proxies` block in `/etc/docker/daemon.json` pointing at the local mihomo
  `127.0.0.1:7890` (a single point of failure — when mihomo is down, builds
  can't pull). Measured 09-30: 7890 alive AND all three registry-mirrors
  directly reachable (they're in no-proxy), and `python:3.12-slim` serves
  200 via `docker.1ms.run` — so builds work TODAY; the durable fix is to
  delete the `proxies` block (needs `systemctl reload docker`, verify whether
  reload applies proxies without a daemon restart; restart needs a safe
  window — `live-restore:true` keeps containers up). Prepared, not applied.
- **GitHub token pool measured (user concern: web-issued tokens expire
  easily)**: all 7 prod tokens alive (GET /user 200, ~4997/5000 core quota)
  and NONE carry the `github-authentication-token-expiration` header → they
  are classic PATs with no date expiry. The real risk is REVOCATION blindness:
  the cooldown registry (`tools/state.py`) is time-based and 401s are never
  cooled or disabled — a revoked token would be round-robined forever,
  silently burning its share of every search. Also: token add/import
  (`POST /api/tokens`) never validates against GitHub and never captures the
  expiry header. Fix designed but not implemented (feature, not a bug): live
  validate at add-time + persist expiry header + disable after N consecutive
  401s with WARN.
- **TLS fingerprint research RESOLVED by live experiment (2026-09-30, DO NOT
  re-litigate)**: the project runs on Python `requests` = OpenSSL ClientHello +
  HTTP/1.1 only — no JA3/JA4 control. A same-proxy/same-source-IP A/B on fnos
  (requests vs curl_cffi `chrome150`, provably different stacks: HTTP/1.1 vs
  h2, JA3 `07ff1e…` vs Chrome) produced IDENTICAL verdicts on groq/cerebras/z.ai.
  **groq's 403 is path-dependent, not fingerprint**: `socks5://` (LOCAL DNS)
  via the trio → normal `401 invalid_api_key`; `socks5h://` (remote DNS) → bare
  403 — 3/3 reproducible across 1080/1090/1091. **cerebras is an IP/ASN-level
  CF wall** (block page on every egress, both clients) — only a residential
  egress could change it. curl_cffi is NOT adopted (falsified premise);
  decision record + probe script: `.omo/plans/curl-cffi-fingerprint-decision.md`,
  `.omo/evidence/ulw-20260930-rootfix/g7_fingerprint_probe/`. Reopen only if a
   provider shows a reproducible same-egress split (requests=403 /
   browser=401). groq's real blocker remains partner auto-revocation (09-24),
   not egress.
