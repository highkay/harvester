# Provider: doubao (Volcengine Ark)

ByteDance Doubao via the Volcengine Ark gateway. The provider code
(`provider/doubao.py`) predates this wave; 2026-10-29 landed the schedule,
config, patterns and tests.

## Endpoints, auth gate and probe model

- Base: `https://ark.cn-beijing.volces.com` — CN endpoint, `use_proxy: false`.
- The provider talks to the pay-as-you-go path `/api/v3`. **Chat completions
  (`POST /api/v3/chat/completions`) is the only auth/verdict gate** — the cheap
  check and every capability-tier probe are chat completions. `/api/v3/models`
  is inspect-only: no verdict in the pipeline reads its status.
- Probe model: `doubao-seed-2-1-pro-260915` — measured 2026-10-29
  (wave0-cn.md probe b) as the latest seed-2.1 id in the `/api/v3` catalog
  page (`https://www.volcengine.com/docs/82379/1330310`, 29 dated doubao ids
  harvested). Replaces the legacy `doubao-pro-32k` alias.
- magpie's own presets mark only `/api/v3` as list-capable; the Coding/Agent
  plan paths serve different catalogs and their model ids (e.g.
  `ark-code-latest`) must NOT be used as probe models here.

## Auth gate (measured 2026-10-29, corrected 2026-11-02)

- `POST /api/v3/chat/completions` with no key or a format-broken key → 401
  `AuthenticationError` ("the API key or AK/SK in the request is missing or
  invalid" / "The API key format is incorrect"). This is the gate the code uses.
- `GET /api/v3/models` answered 401 for the same two cases, but it is **not**
  the verdict source; the old wording that called `/models` the auth gate was
  wrong.
- Why a `/models` 200 never proves a key is alive: measured 2026-11-02 on the
  DashScope Coding-plan endpoints (`probe-sksp-candidate.jsonl`), an invalid
  `sk-sp` key got `/models` 200 on both `coding.dashscope.aliyuncs.com` and
  `coding-intl.dashscope.aliyuncs.com` while chat answered 401
  `invalid_api_key`. That measurement is another vendor's plan path, not Ark's,
  but it is the reason every OpenAI-compatible provider here gates on chat
  completion and keeps `/models` for inspection only.

## Status map (pinned by tests/test_doubao_provider.py)

- 404 (unknown deployment / model id) → NO_MODEL → wait-check. A wrong probe
  id never condemns the key.
- 401 → INVALID_KEY.
- 403 `AccountOverdueError` / `account overdue` / `arrearage` → NO_QUOTA
  (measured 2026-11-02 on two held wait-check samples: an authentic key with
  an unpaid bill answers 403 `AccountOverdueError`, `type: Forbidden`). Plain
  403 stays NO_ACCESS.
- 402 / arrearage body → NO_QUOTA (authentic key without funds).
- 429 `insufficient_quota` → NO_QUOTA; plain `rate_limit_exceeded` →
  RATE_LIMITED (retryable).
- 5xx / empty 200 body → SERVER_ERROR (retryable), never UNKNOWN.

## Key shape (measured, not assumed)

Ark API keys are UUID v4: lowercase hex 8-4-4-4-12. Lane sampler over GitHub
code search (2026-10-29, text_matches, shapes only, keys masked): every real
literal captured under `ARK_API_KEY=` / volces Bearer contexts was a lowercase
UUID (16/16 UUID-form captures; 0 other shapes after placeholder exclusion).
`ep-XXXXXXXXXXXXXX-XXXXX` ids are deployment/model endpoints, not keys — 'p'
is not hex, so the UUID pattern cannot capture them.

## Corpus and supply outlook

- `"ARK_API_KEY"` GitHub code-search corpus: 60,928 (wave-0 measurement,
  2026-10-29); re-measured 59,904 by this lane's sampler the same day.
- `volcengine_ark_api_key` is under GitHub PUSH PROTECTION (secret-scanning
  partner notification), so fresh supply is suppressed; the residual corpus
  is old commits and bypasses. Expect LOW yield — weekly floor cadence
  (`0 5 * * tue`), not a daily slot.
- `VOLCENGINE_API_KEY` anchor measured 5,752 hits (2026-10-29); env-anchored
  extraction covers both names.

## Capability tiers and push wiring (2026-11-02)

- `capability_tiers`: `seed` probes `doubao-seed-2-1-pro-260915`;
  `thirdparty` probes `deepseek-v4-1-flash-261017` (Ark model list fetched
  2026-11-02). The cheap gate runs first, then every tier gets its own
  `max_tokens=1` completion probe.
- Only an OK tier admits the key to `valid-keys-<tier>.txt`; the union
  `valid-keys.txt` still receives every valid key. A non-OK tier never
  cancels another tier's OK result. If no tier is OK but at least one is
  RETRYABLE, the key goes to wait-check rather than permanent invalid.
- fnos mappings: base `doubao` -> group 24, `doubao:seed` -> group 27,
  `doubao:thirdparty` -> group 28. With tier files present, push fans out
  to the tier groups and does not also push the union. A legacy run with
  only the union file still uses the base group when every tier file is
  empty or absent.
- Production measurement on the first five harvested keys: two seed-only,
  one dual-tier, one seed-OK/thirdparty-RETRYABLE, one dead. The real
  tiered push added four keys to group 27 and one to group 28. Evidence:
  `.omo/evidence/capability-20261030/prod-measure.md`.

## References

- `.omo/evidence/magpie-coverage/wave0-cn.md` — probe (b) ark model id +
  401 gate evidence.
- Lane sampler (this todo's evidence file
  `.omo/evidence/magpie-coverage/todo6-ark.md`) — UUID v4 shape
  verification, corpus totals.

## Depth gate, verdict cache and measured yield (2026-11-03)

`doubao` is inside `VERDICT_CACHE_TASKS`: an explicit `INVALID_KEY` is reused
for 30 days and an explicit `NO_QUOTA` for 14 days; anything else is re-probed
on the next pass. The cache key includes task, key, normalized base URL,
endpoint, probe model and tier, so a region/model change can never produce a
false hit.

Depth stays at `max_links_per_run: 9000`. The pre-registered gate for raising it
to 12,000 required `gather_304_empty >= 50%` of processed links; measured
values were 41.92% (natural run `270a3911-70af-4e20-bbba-8a9991c6ce0f`, 5799 /
13835), 48.36% (`e865288f`, 8012 / 16566) and 41.06% (`ef06ce7b`, 6079 /
14804). All three windows fail, so the config was left byte-identical and no
12,000 canary ran; `yield_causality` remains UNKNOWN. Evidence:
`.omo/evidence/mimo-qwen-doubao-yield-optimization-execution/task-13-doubao-depth.json`.

Measured production yield through 2026-11-03: 4 runs, 11 lifetime valid keys,
first run 2026-10-29. The provider is young and thin — do not read a single
zero-valid run as a verdict on the provider. The 2026-11-03 efficient pass
(`ef06ce7b-72d1-4cda-98ac-f048e4c48697`, 1200s, 508 checked, 21,423 links,
1,279 materials) produced 2 valid keys; it is a manual probe, not a natural
cycle.
