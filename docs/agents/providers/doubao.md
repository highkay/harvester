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

## Push / wiring

Scheduled (seed row in `web/scheduler.py`); gpt-load group wiring lands with
the wave-3 wiring todo (plan todo 20), validation model
`doubao-seed-2-1-pro-260915`.

## References

- `.omo/evidence/magpie-coverage/wave0-cn.md` — probe (b) ark model id +
  401 gate evidence.
- Lane sampler (this todo's evidence file
  `.omo/evidence/magpie-coverage/todo6-ark.md`) — UUID v4 shape
  verification, corpus totals.
