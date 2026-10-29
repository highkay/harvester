# agnes-ai provider - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Feature: agnes-ai provider (scan + validate + push)

- `provider/agnes_ai.py`: `AgnesAIProvider` (registered as `"agnes-ai"`). Agnes
  AI is an OpenAI-compatible omni-modal gateway at
  `https://apihub.agnes-ai.com/v1`, authed with `sk-` Bearer keys. Validation
  is a minimal chat-completion probe: `POST /chat/completions` with
  `{"model":"agnes-2.5-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":1}`.
- **`GET /v1/models` trap**: the endpoint answers 200 for ANY Bearer token
  (presence-only, live-probed), so it is NOT used for validation; `inspect()`
  only lists model IDs for keys that already passed `check()`.
- Status map (`_judge_chat`): 200 + JSON → valid; 401 (or body `无效的令牌` /
  `invalid api key|token`) → INVALID_KEY; 402 → NO_QUOTA; 429 → RATE_LIMITED;
  403 → NO_ACCESS; 400 → BAD_REQUEST; >=500 → SERVER_ERROR; 200 non-JSON →
  UNKNOWN. Keys are `sk-` (length undocumented, no fixed-width pattern).
- Extraction (in `examples/config-agnes-ai.yaml` + `config/defaults.py`
  preset): context-anchored. Env-name assignments (`AGNES_API_KEY` /
  `AGNES_AI_API_KEY` / `AGNES_KEY` / `agnes[_-]?(ai[_-]?)?api[_-]?key`) at
  task level, with a negative lookahead excluding `sk-ant-` / `sk-proj-` /
  `sk-svcacct-`. The `apihub.agnes-ai.com` domain dorks
  (`"apihub.agnes-ai.com"`, plus `"Authorization"` and `"agnes-ai.com"
  "api_key"` variants) widen to a Bearer/quoted `sk-` pattern per condition.
- Push: `web/agnes_ai_push.py` `AgnesAIPushService` (mirror of
  `web/serpapi_push.py`), env-gated by `AGNES_LOAD_BASE_URL` /
  `AGNES_LOAD_GROUP_ID` / `AGNES_LOAD_AUTH_KEY`. After an agnes-ai scan it
  POSTs validated `sk-` keys to a gpt-load instance
  `POST {base}/api/keys/add-multiple` with body
  `{"group_id":<int>,"keys_text":"<key>\n<key>"}`, chunked at 500 keys per
  POST. Defaults: `AGNES_LOAD_BASE_URL=http://107.172.141.203:43001` (the
  user's gpt-load instance; 107.172.141.203 is the VPS public IP and LAN
  192.168.1.18 is the same host), `AGNES_LOAD_GROUP_ID=19`,
  `AGNES_LOAD_AUTH_KEY=""` (empty → no Authorization header; non-empty →
  `Bearer <key>`). Only generic `sk-` keys are pushed (`sk-ant-`/`sk-proj-`/
  `sk-svcacct-` excluded); never raises; idempotent per run_id; writes one
  `push_logs` row (gpt_load_config_id=0, group_id=env int).
- Hook: `web/runner.py` `_on_completed` fires agnes-ai push iff
  `provider_name == "agnes-ai"` (daemon thread, ImportError-safe).
- Schedule: `("agnes-ai", "35 */6 * * *", "examples/config-agnes-ai.yaml")`
  in `web/scheduler.py`; seeds only into an EMPTY `schedule_config` table, so
  prod (fnos) needs a one-off
  `INSERT INTO schedule_config (provider_name, cron, enabled, config_file) VALUES ('agnes-ai','35 */6 * * *',1,'examples/config-agnes-ai.yaml')`
  (or a UI add) after deploy.
- Redaction: NO `tools/patterns.py` entry (a bare `sk-` pattern would blitz
  logs); the provider never writes keys to disk/logs.
- **Egress & DNS trap (measured on fnos 2026-09-04)**: `apihub.agnes-ai.com`
  DNS is polluted inside the container, so plain `socks5://` (local DNS) and
  container-direct both fail with ConnectionError/NETWORK_ERROR while
  `socks5h://` (remote DNS at the tunnel) returns 200. This silently poisoned
  the first 3 agnes runs (~110 candidates: every check was NETWORK_ERROR, yet
  landed in `invalid-keys.txt` — indistinguishable from real invalid keys
  until a REAL valid key was e2e-probed). Pin agnes to the socks5h rotation
  via `HARVESTER_PROXY_AGNES_AI` (compose default:
  `socks5h://192.168.1.18:1080,1090,1091`). `search/client.py` and
  `config/schemas.py` both whitelist `socks5h` (with mandatory port). The
  trap also cost 0 real keys in run 1-3; a live probed REAL key (`sk-` + 48
  alnum — measured 2026-09-04) returns 200 via socks5h from the container.
- Always measure real-key egress per PROVIDER (not just from the host) before
  trusting invalid counts — host-direct success does NOT imply container
  success (groq taught 403-blocks; agnes adds DNS pollution + socks5h).
