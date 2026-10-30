# Provider: mistral

Read before: mistral scans/validation/push, or touching
`provider/mistral.py` / `examples/config-mistral.yaml`.

## The models endpoint is a real auth gate (measured 2026-10-29, wave-0 US probe pack)

`GET https://api.mistral.ai/v1/models` answers **401
`{"detail":"Invalid API Key"}`** in 1.4-1.6 s for a missing key AND for a
bogus 32-char key. Measured from the prod container through both socks exits
(`1090`, `1091`) and the spare `1080`. The body is Mistral's own JSON, not a
Cloudflare interstitial — Cloudflare sits in front (`server: cloudflare`), but
the response comes from the application layer ahead of any model lookup. So
the models list gates auth and is not the openrouter/ollama public-list trap.
The bad key also got 401 on `POST /v1/chat/completions`, so no
`misleading_success_output` case exists here.

Caveat from the same probe pack: the two "US" socks exits actually egress
through Cloudflare addresses that ipinfo geolocates to CN (Shanghai), and
`api.openai.com` is refused from them. Mistral and Gemini answer normally
through the same exits, so this does not change the mistral verdict — but do
not read a 403 from a different host as a mistral auth signal without
checking the egress identity first.

## Key shape: 32 alnum chars, no prefix (measured 2026-10-29)

Four unique real values, all **32 chars of mixed `[A-Za-z0-9]`**, no `-`, no
`_`, no prefix:

| length | charset | prefix |
| --- | --- | --- |
| 32 | upper + lower + digits | none |

Sampling reached only 4 real values out of ~250 hit pages because the corpus
is placeholder-dominated (`your_mistral_api_key`, `your-mistral-key`,
`XXXXXXXXX`, `<mistral token>`, `your_api_key`, empty values; most `.env`
hits are `.env-example` / `.env_sample`). About 4 in 250 pages carried a real
value.

Consequence for the pattern: a prefix-less 32-char alnum string cannot be
grepped on its own — hashes and ids share the shape. Extraction is therefore
env-anchored (`MISTRAL_API_KEY` / `mistral_api_key`) plus domain-anchored
(`api.mistral.ai`, Bearer/quoted forms), the serpapi env+URL anchoring
precedent. The body class is exactly `[A-Za-z0-9]{32}` with a trailing
boundary guard, and a placeholder lookahead excludes the forms above.

## Probe model

`default_model: mistral-small-latest`. Its liveness was **not** verified:
wave-0 only probed the auth layer (no live key was available), so the id is an
assumption taken from the public model list naming convention. `_judge` maps
404 and 422 (and a model-missing 400) to NO_MODEL / BAD_REQUEST, which
CheckStage files in the wait-check bucket — a retired id costs re-checks,
never a key discard. The pilot run must confirm the id against a live key's
`/v1/models`.

## Judge mapping (provider/mistral.py)

- 401 -> INVALID_KEY (permanent); this is the measured gate status
- 402 -> NO_QUOTA, 403 -> NO_ACCESS (both inherited from the base judge)
- 429 -> split by body: rate-limit wording stays RATE_LIMITED (retryable),
  quota / credit / billing / balance markers -> NO_QUOTA
- 404 / 422 (and 400 with a model-missing marker) -> NO_MODEL (wait-check)
- 400 / 422 otherwise -> BAD_REQUEST (wait-check: a malformed probe is not a
  key verdict)
- 200 with an error body -> markers decide: auth -> INVALID_KEY, quota ->
  NO_QUOTA, anything else -> BAD_REQUEST
- empty or unparseable 200 body -> SERVER_ERROR (retryable), never UNKNOWN —
  CheckStage files UNKNOWN as a permanent invalid discard

## Ops

- `use_proxy: true` — api.mistral.ai is reached through the egress pool.
- Egress pin (2026-10-30): plain `socks5://` to api.mistral.ai fails with
  SSLError, so every check lands in the retryable `network_error` bucket
  (run `179860a9` wedged at wait-check, 0 invalid); `socks5h://` (remote DNS)
  answers 401 for a bogus key (measured on all three exits 1080/1090/1091).
  Both compose files now default `HARVESTER_PROXY_MISTRAL` to
  `socks5h://192.168.1.18:1090` so mistral pins a dedicated socks5h host
  instead of inheriting the global plain socks5 rotation.
- `rate_limit` 0.2/2 (hardened-provider pace, 5 s per key).
- `global.max_links_per_run: 9000`, `search_types: [code]`.
- Schedule seed: `0 5 * * sun` (weekly floor). Sunday 05:00 follows deepseek
  02:00 (capped by depth) and does not overlap serpapi's mon/wed/fri 06:00.
- Corpus anchors measured 2026-10-29: `MISTRAL_API_KEY` 159,744 hits,
  `MISTRAL_API_KEY filename:.env` 14,912.
