# Provider: doubao (Volcengine Ark)

ByteDance Doubao via the Volcengine Ark gateway. The provider code
(`provider/doubao.py`) predates this wave; 2026-10-29 landed the schedule,
config, patterns and tests.

## Endpoints and probe model

- Base: `https://ark.cn-beijing.volces.com` — CN endpoint, `use_proxy: false`.
- The provider talks to the pay-as-you-go path `/api/v3` (chat completions +
  models list). magpie's own presets mark only `/api/v3` as list-capable; the
  Coding/Agent plan paths serve different catalogs and their model ids
  (e.g. `ark-code-latest`) must NOT be used as probe models here.
- Probe model: `doubao-seed-2-1-pro-260915` — measured 2026-10-29
  (wave0-cn.md probe b) as the latest seed-2.1 id in the `/api/v3` catalog
  page (`https://www.volcengine.com/docs/82379/1330310`, 29 dated doubao ids
  harvested). Replaces the legacy `doubao-pro-32k` alias.

## Auth gate (measured 2026-10-29)

- `GET /api/v3/models` no key → 401 `AuthenticationError` ("the API key or
  AK/SK in the request is missing or invalid").
- Same endpoint with a format-broken key → 401 `AuthenticationError` ("The
  API key format is incorrect").
- `POST /api/v3/chat/completions` with a bad key → 401.
- Clean gate, no public-200 trap.

## Status map (pinned by tests/test_doubao_provider.py)

- 404 (unknown deployment / model id) → NO_MODEL → wait-check. A wrong probe
  id never condemns the key.
- 401 → INVALID_KEY.
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
