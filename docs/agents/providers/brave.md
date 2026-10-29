# brave provider - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Feature: brave provider (scan + validate + push)

Landed 2026-10-15/16 as five atomic commits, all on `main` and all deployed to
fnos: `56397d9` provider → `21b2d96` config/preset → `4491662` push + runner
hook → `223c165` schedule seed + compose/env → `522e088` egress default. Plan
`.omo/plans/brave-search-provider.md`; evidence `.omo/evidence/brave/`
(`supply-probe.md`, `t2-provider.md`, `t3-config.md`, `t3a-pattern-fix.md`,
`t4-push.md`, `t6-integrate.md`, `t7-deploy.md`, `t8-egress.md`,
`t8a-compose.md`, `t8a-redeploy.md`, `t9-probe-freeness.md`).

- `provider/brave.py` — `BraveProvider` (registered as `"brave"`; import +
  `__all__` in `provider/__init__.py`). Base `https://api.search.brave.com`,
  auth is the `X-Subscription-Token` header (no Bearer scheme, no query param).
- **Validation is a SINGLE-STAGE ZERO-COST probe** (deliberate deviation from
  tavily's two stages, which exist only because tavily's free `/usage` cannot
  see a disabled account): `GET {base}/res/v1/web/search` **without `q`** and
  without a `params`/`json` kwarg. Brave authenticates BEFORE request
  validation, so the 422 "missing q" answer proves the token authenticated and
  is never billed — a wide extraction corpus therefore costs nothing to
  validate. `inspect()` reuses the SAME probe (no extra request) and harvests
  the POSITIONAL `X-RateLimit-*` month window (`x-ratelimit-policy`
  `1;w=1, 2000;w=2592000` → index of `w=2592000`, then Limit/Remaining/Reset
  by position; `limit 0` = unlimited). Missing/short/garbage headers → no quota
  items, never an exception. The probe BODY is deliberately NOT flattened into
  the audit (tavily's `_flatten_audit` shape): a header-validation entry can
  echo the submitted token and audit items are written to disk — only
  `quota.*`, `probe.status_code`, `probe.error_code` are emitted.
- **422 taxonomy** (`_judge_probe`: error code first, then status; source =
  hikari's live-probed `classify_brave_failure` + prod wire evidence
  2026-10-15/16):
  - 422 `VALIDATION` with `meta.errors[].loc != ["header","x-subscription-token"]`
    (or no `meta` at all) → **valid, unbilled**. MEASURED on prod with a real
    key (t9): `loc=[["query","q"]]`, `msg:"Field required"`, 641-byte nested
    `{"type":"ErrorResponse","error":{…}}` envelope, judge `ok=True`.
  - 422 `VALIDATION` with `loc` pointing at the token header (case-insensitive)
    → INVALID_KEY.
  - 422 `SUBSCRIPTION_TOKEN_INVALID` → INVALID_KEY. **Brave's real auth
    rejection is 422, NOT 401** — measured twice with a bogus token (workstation
    direct 1.24 s, fnos container via socks5h).
  - 402 `QUOTA_LIMITED` (and bare 402) → NO_QUOTA → `no-quota` bucket;
    recovers at the UTC month boundary, so it is a wait verdict, never a key
    verdict, and such keys are never pushed.
  - 429 `RATE_LIMITED` (and bare 429) → RATE_LIMITED → wait-check (the key is
    valid but throttled).
  - 401/403 **with** a Brave JSON envelope (`code`/`title`/`type`/`detail`)
    → INVALID_KEY; 401/403 **non-JSON** (Cloudflare/geo HTML interstitial,
    empty body) → NO_ACCESS → wait-check, i.e. an egress problem that must not
    burn the candidate.
  - 200 + parseable JSON (dict or list) → success (defensive; a q-less probe
    should never 200); 200 non-JSON → UNKNOWN; 400 → BAD_REQUEST; >=500 →
    SERVER_ERROR; timeout/connection (`code == 0` sentinel, body `"timeout"`) →
    TIMEOUT / NETWORK_ERROR; unrecognized or garbage 422 code → UNKNOWN.
    UNKNOWN, 200-non-JSON and garbage all land in **invalid** (house rule:
    undecidable → invalid, never push an unproven key); every retryable reason
    lands in wait-check. Blank token → INVALID_KEY with **zero requests sent**.
  - Envelope-shape agnostic on purpose: `_error_code()` reads nested
    `error.code` (what the live wire sends) AND flat `code` (the documented
    shape); `_validation_errors()` reads `meta.errors` and `error.meta.errors`.
    The body comes from `error.response.text`, NOT `http_error_message` —
    `search/client.py` truncates at 300 chars and a VALIDATION envelope's
    decisive `loc` tail sits at ~290-400 chars, so truncation would file live
    keys as UNKNOWN → invalid. Pinned by
    `test_422_long_body_survives_the_300_char_truncation`.
- **Egress lesson (measured from the fnos `harvester-web` container,
  2026-10-16 ~01:00, `t8-egress.md`)**: direct = DEAD (ConnectTimeout 2/2
  after 30 s, no TCP path to `api.search.brave.com:443`); the plain
  `socks5://192.168.1.18:1080/1090/1091` trio = TLS `certificate verify failed
  — Hostname mismatch` on ALL THREE (the container resolves the hostname
  through poisoned LOCAL DNS and the proxy merely relays to that bogus IP — a
  client-side artifact, not an exit problem); `socks5h://` (remote DNS) on the
  same three ports = clean `422 SUBSCRIPTION_TOKEN_INVALID` in 1.95-3.33 s.
  **`HARVESTER_PROXY_BRAVE` now defaults to the socks5h trio in BOTH compose
  files** (`522e088`, one added line each, mirroring the
  `HARVESTER_PROXY_AGNES_AI` precedent):
  `socks5h://192.168.1.18:1080,socks5h://192.168.1.18:1090,socks5h://192.168.1.18:1091`.
  Container env is frozen at creation → this needs a **recreate**, not a
  restart; fnos `.env` carries no `HARVESTER_PROXY_BRAVE` key, so the compose
  default wins unopposed (a `.env` value still overrides it). Same family as
  the agnes-ai DNS-pollution trap (2026-09-04) and the groq 403 trap: measure
  real-key egress per PROVIDER from inside the container before trusting any
  invalid count.
- **Residual egress risk (flagged, deliberately UNFIXED)**:
  `search/client.py::set_proxy` builds failover candidates as
  `[this scan's exit, *proxy_rotation()]`, and `proxy_rotation()` returns the
  GLOBAL `HARVESTER_PROXY` — the three plain-`socks5://` exits. A pinned brave
  run therefore carries
  `[socks5h://…:1080, socks5://…:1080, socks5://…:1090, socks5://…:1091]`, and
  either guard can rotate the process-wide egress back onto DNS-poisoned plain
  socks5: `_note_proxy_transport` (3 consecutive transport failures — an SSL
  handshake failure counts) or `_note_proxy_status` (an unexplained-403/429
  streak across >=2 distinct hosts). Brave's own 422s cannot feed the streak
  (422 is not in `_PROXY_DEGRADED_STATUSES`) but concurrent GitHub 403s plus
  one other host can. **This affects the agnes-ai pin identically.** The
  permanent fix is a scheme-consistent candidate list (offer only the `socks5h://`
  siblings when the pin is `socks5h://`) — a `search/client.py` change touching
  every provider, so it was NOT attempted. Observed: 0 failover / rotation /
  degraded log lines in the fixed run's first 12 minutes.
- **Extraction** (`config/defaults.py` brave preset + `examples/config-brave.yaml`,
  defaults↔example lockstep-pinned): task level is env-anchored only —
  `brave[_-]?(search[_-]?)?api[_-]?key` / `brave[_-]?(subscription[_-]?)?token`
  + `["'\]]{0,2}\s*[:=]\s*["']?([A-Za-z0-9_\-]{16,})`, ONE capture group, **no
  width ceiling and no prefix assumption**. Five dorks: `"BRAVE_API_KEY"`,
  `"BRAVE_SEARCH_API_KEY"`, `"api.search.brave.com"`,
  `"X-Subscription-Token" "brave"`, `"brave" "subscription token" extension:env`.
  Dorks 3 and 4 carry the per-condition WIDE override
  `(?i)(?:X-Subscription-Token["'\s:]*|Bearer\s+|["']\s*)(?!x-subscription-token)([A-Za-z0-9_\-]{20,})`.
  - **Self-match defect, measured and fixed (`t3a-pattern-fix.md`)**: the
    bare-quote branch matched the OPENING quote of a curl
    `-H "x-subscription-token: <key>"` line and the 20-char charset then ate
    the header name itself (`x-subscription-token` is exactly 20 charset-legal
    chars), so the "key" candidate was the literal header name. Fixed by
    inserting the `(?!x-subscription-token)` lookahead immediately before the
    capture group in BOTH files. The same pass found `config/defaults.py`'s
    raw-string pattern carrying DOUBLED backslashes (`Bearer\\s+` = literal
    `\s+` in the regex), so its Bearer and quoted-token branches never fired;
    escapes normalized → defaults and example are now byte-identical patterns.
  - **Supply measured 2026-10-15** (GitHub code search `total_count`, 6.5 s
    spacing): dork1 72,832 + dork2 24,960 + dork3 27,520 + dork4 23,168 +
    dork5 **0** = **148,480**. Dork 5's `extension:env` file-class qualifier
    matches nothing for those quoted terms — genuine scarcity, not a defect, so
    the dork is KEPT (if it is ever re-tested, try `filename:.env`). Total
    >=10k → the daily tier of the plan's frequency ladder.
  - **Key format MEASURED, n=1** (prod `valid-keys.txt`, `t9-probe-freeness.md`
    §4): prefix **`BSA`**, length **31** (NOT 32), charset `[A-Za-z0-9_-]` with
    both `-` and `_` present (6 digits / 13 upper / 9 lower / 3 special).
    **Tripwire: re-align the pattern only when the corpus grows.** A
    `BSA[…]{29}` fixed-width tightening on n=1 is guesswork and a `{32}`
    assumption would already miss this key — the serpapi fixed-32 lesson. Never
    add an upper bound.
  - Redaction: still **NO `tools/patterns.py` entry** (a bare pattern would
    blitz logs); revisit at n>=3 with `r"\bBSA[0-9A-Za-z_-]{20,}"`, because
    today any brave key that reaches a log line is written verbatim. The
    provider and the push service never write keys to disk or logs.
- **Push**: `web/brave_push.py` `BravePushService` (+ module singleton
  `get_brave_push_service()`), mirroring `web/serpapi_push.py`'s per-key form —
  hikari's `POST /api/keys/batch` is Tavily-only, so brave cannot use it.
  Env-gated by `BRAVE_PROXY_BASE_URL` / `BRAVE_PROXY_AUTH_KEY`: base empty =
  silent no-op (0 HTTP calls, no row); the auth key is **optional** (Bearer sent
  only when non-empty) because prod hikari runs `DEV_OPEN_ADMIN=true`. Per key:
  `POST {base}/api/keys`, body exactly `{"api_key": k, "provider": "brave"}`,
  timeout 30 s, ×3 retry with 1/3/9 s backoff on 429/5xx, fail-fast on 401/403.
  **hikari answers 201 for new, existing AND undeleted keys**, so net-new =
  the provider-scoped `GET {base}/api/keys?provider=brave&per_page=1` `total`
  delta read before/after the loop, clamped to `[0, accepted]`; if either total
  read fails → `added = accepted` plus `"pool count unavailable; added counted
  from accepted responses"` in `push_logs.error_message` (never silent).
  **409 `provider_conflict`** (the key already lives under another provider) =
  **ignored, not failed**. No key-shape pre-filter (the format was unmeasured
  when this was written). Exactly one `push_logs` row per run
  (`gpt_load_config_id=0`, `group_id=0`), per-run idempotent (`_seen_run_ids` +
  lock, checked before any I/O), `push_valid_keys` NEVER raises.
- Hook: `web/runner.py::_dispatch_provider_pushes` fires the brave push iff
  `provider_name == "brave"` (daemon thread, ImportError-safe, appended after
  the opencode block). The generic gpt-load push self-skips because there is no
  `provider_group_mapping` row for brave — deliberately not added.
- Schedule: `("brave", "0 15 * * *", "examples/config-brave.yaml")` in
  `web/scheduler.py::_DEFAULT_SCHEDULES` — daily 15:00 with **no day-of-week
  field on purpose** (the seed stays daily; a supply probe may only adjust the
  PROD row). Seeds fill an EMPTY `schedule_config` only, so prod got a one-off
  `INSERT … ON CONFLICT(provider_name) DO NOTHING` → **row id 92**
  (`0 15 * * *`, enabled, `examples/config-brave.yaml`, next fire 2026-10-16
  15:00 CST); prod table 21 → 22 rows, startup log `Scheduled brave with cron
  '0 15 * * *'`, `Scheduler started with 11 job(s)`. The `config/defaults.py`
  preset ships `enabled: False`; `examples/config-brave.yaml` is
  `enabled: true`. Rate limit 0.2 req/s + burst 2 adaptive (hardened-provider
  pace, inherited-from-lineage rather than measured — the lockstep test surfaces
  any drift).
- Deploy (fnos, `t7-deploy.md` + `t8a-redeploy.md`): backup → `git fetch` →
  `LOCAL_ONLY_COUNT=0` gate → `git reset --hard origin/main` → restore the
  fnos-local `Dockerfile.web` (Tsinghua PyPI mirror) +
  `cp docker-compose.hostnet.yml docker-compose.yml` → safe-window guard on
  `run_records status='running'` → `docker compose up -d` (RECREATE, for env) →
  whole-tree cp overlay → `restart` → `/health` ok → **6/6 md5 host↔container
  MATCH** (`provider/brave.py`, `web/brave_push.py`, `web/runner.py`,
  `web/scheduler.py`, `config/defaults.py`, `examples/config-brave.yaml`),
  identical to the workstation's md5s at the same SHA. Rollback copies:
  `rollback-20261016-002744/`, `rollback-20261016-013456/`.
- **QA facts**: full suite at merge = **1172 OK / 8 skipped**
  (`Ran 1172 tests in 81.441s`, rc 0, `.omo/evidence/brave/t6-full-suite.log`),
  i.e. the ~1059 baseline + exactly the wave's 113 new tests, 0 regressions.
  **Probe-freeness (plan QA-5) = PASS-WITH-NOTE**: two probes 11.59 s apart
  through the DEPLOYED class against the run's first valid key both answered
  `422 VALIDATION` / `loc=["query","q"]` with **no `X-RateLimit-*` and no
  `retry-after` header at all** (7 headers total: connection, content-encoding,
  content-type, date, server-timing, transfer-encoding, vary), so freeness
  cannot be instrumented positively — it rests on the wire-observed
  auth-precedes-routing ordering plus the run's OWN `inspect()` 891 s earlier,
  which persisted `probe.status_code: 422` / `probe.error_code: VALIDATION` and
  zero `quota.*` items to `summary.json`. Consequence: the positional
  `X-RateLimit-*` parser stays untested against a real Brave sample because no
  such sample exists on this code path. Those probes also closed the
  "valid-key success branch never measured" gap and showed the token is NOT
  echoed in the body.
- **First runs**: the scheduler's catch-up fired run `6ba06414` 60 s after the
  T7 restart (the row was inserted after that day's 07:00 UTC fire) and it was
  **sacrificed to the egress fix** — `HARVESTER_PROXY_BRAVE` was still empty, so
  the run inherited the plain-socks5 global trio and every validation died on
  the hostname-mismatch certificate; reconciled `failed / interrupted by
  service restart` (3788 s, 0 valid, 0 checked), its `wait-check` backlog stays
  on disk and is re-checkable. The fixed run
  **`ecb6e000-4a0e-4bea-9833-8e94a74086d1`** (triggered via
  `POST /api/schedule/brave/run` → 202, started 2026-10-15 17:38:09 UTC,
  runtime YAML `proxy: socks5h://192.168.1.18:1080`) was **still RUNNING at
  docs-writing time (2026-10-15 18:36 UTC / 2026-10-16 02:36 CST)** with early
  counters: 143+ check verdicts at `errors 0`, **0 TLS/SSLError lines in the
  first 12 minutes**, no `wait-check-keys.txt` created at all, and by ~02:00
  CST **1 valid / 900+ invalid** (re-read 02:33 CST: `valid-keys.txt` 1 line,
  `invalid-keys.txt` 1382 lines, `links.txt` 68,221 lines, `push_logs` still
  empty — the push fires on completion). **The final yield line gets updated by
  the
  closeout pass** (plan T10 / QA-4: reconcile the `push_logs` row against
  hikari's `GET /api/keys?provider=brave` total growth). Do not
  restart/recreate `harvester-web` while it is live.
- Tests: `tests/test_brave_provider.py` (**64**), `tests/test_brave_pattern.py`
  (**7**), `tests/test_brave_rate_limit.py` (**3**),
  `tests/test_web_brave_push.py` (**36**), `tests/test_web_runner_brave.py`
  (**3**) = 113; seed pins added in `tests/test_web_scheduler.py`. All five
  re-run green in the docs worktree at `522e088` (2026-10-15).
- Known non-defects noted while tracing (`t9-probe-freeness.md` §6; pre-existing,
  NOT fixed): `_judge_probe` reports `reason=UNKNOWN` on success because
  `CheckResult.success()` leaves the default reason (routing keys on `ok`);
  `summary.json` keys its `models` map by the raw key value (mode `0600`,
  shared storage-layer behaviour); `valid-keys.txt` is mode `0644`, not `0600`.
