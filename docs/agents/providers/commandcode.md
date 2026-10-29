# Provider: commandcode

Read before: commandcode scans/validation/push, or touching
`provider/commandcode.py` / `examples/config-commandcode.yaml`.

## The models endpoint is a PUBLIC trap (measured 2026-10-29, wave-0 CN probe pack)

`GET https://api.commandcode.ai/provider/v1/models` returns the **same
18,461-byte 200** model list for a missing key and for a bad key
(`sk-0000…`). Byte-identical across repeats. A 200 there proves nothing —
never judge a key by it (same trap family as openrouter `/v1/models`,
ollama `/v1/models`, agnes-ai presence-only `/v1/models`).

The trusted verdict path is a minimal chat completion:

```
POST https://api.commandcode.ai/provider/v1/chat/completions
Authorization: Bearer <key>
{"model": <probe>, "messages": [...], "max_tokens": 1}
```

- bad key -> **401** `{"error":{"message":"Invalid 'Authorization' header or
  token.","type":"authentication_error","code":"UNAUTHORIZED"}}` -> INVALID_KEY
- `provider/commandcode.py` maps: 401 -> INVALID_KEY, 402/quota-body ->
  NO_QUOTA, 429 split by body (quota markers -> NO_QUOTA, else RATE_LIMITED),
  404 -> NO_MODEL (probe model retired upstream -> wait-check, never invalid),
  5xx/timeout/unparseable-or-empty 200 -> SERVER_ERROR/TIMEOUT (retryable
  wait-check, never UNKNOWN).

Note the base is `/provider/v1`, NOT `/v1` (magpie presets.go agrees).

## Key shape: UNDETERMINED (2026-10-29)

- Official docs (`https://commandcode.ai/docs/provider`, 504,595 B) use
  `Authorization: Bearer <CMD_API_KEY>`; `sk-` appears **0 times** -> no
  `sk-` prefix assumption.
- Six GitHub code-search queries (`COMMANDCODE`, `COMMANDCODE_API_KEY`,
  `CMD_API_KEY`, `api.commandcode.ai`, `COMMANDCODE sk-`,
  `commandcode.ai Bearer`) x 10 text-match samples carried **zero real key
  literals** — corpus totals exist (COMMANDCODE 152,832; CMD_API_KEY 814;
  api.commandcode.ai 4,648) but are doc/repo-name noise.
- Therefore the pattern is env-anchored (`CMD_API_KEY` /
  `COMMANDCODE_API_KEY`) plus domain-anchored (`api.commandcode.ai`) with a
  wide body class `[A-Za-z0-9_\-]{20,}` and a `your_` placeholder guard.
  Revisit the body class after the first pilot run measures real keys.

## Probe model

`default_model: google/gemini-3.1-flash-lite` — the cheapest-looking chat
tier in the public catalog (94 ids fetched 2026-10-29). The id comes from
the trap endpoint's list, which is fine for *picking a probe target* (the
list itself is genuinely served), just never for judging keys. If it starts
answering 404 the keys route to NO_MODEL/wait-check — swap in a current id
from the catalog.

## Ops

- `use_proxy: true` — US-hosted endpoint, route via the egress pool. The
  first no-key probe hit a 20s ReadTimeout once (retry succeeded): keep the
  request timeout set.
- Schedule seed: `0 5 * * mon` (weekly floor, house rule).
- rate_limit 0.2/2 (hardened-provider pace), `max_links_per_run: 9000`.
