# modelscope provider - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Feature: modelscope provider (scan + validate + push)

- `provider/modelscope.py`: `ModelScopeProvider` (registered as `"modelscope"`).
  Validation is `GET https://modelscope.cn/openapi/v1/users/me` with
  `Authorization: Bearer <token>` (single CN hub endpoint; clean status map).
- Status map (`_judge`): 200 + `success:true` + `data` dict → valid; 401 (or
  body `InvalidAuthentication`) → INVALID_KEY; 403 → NO_ACCESS; 429 →
  RATE_LIMITED; >=500 (post-retry) → NETWORK_ERROR; 200 without proper
  JSON/data → UNKNOWN (presence-only trap guard).
- Traps: `GET https://api-inference.modelscope.cn/v1/models` returns 200 for
  ANY key (presence-only) — never used for validation; `POST /api/v1/login`
  returns 400 + business Code 10010103009 for invalid tokens (not used as
  primary because of read-only-tier subtleties).
- Extraction: context-anchored — task-level env-name assignments capturing
  `ms-[A-Za-z0-9_-]{8,}` (no naked branch — the old bare `[0-9A-Za-z_-]{20,}`
  flooded the check stage with 2648+ candidates / 0 valid on 2026-08-11);
  domain-anchored conditions add Bearer/quoted variadic + `oauth2:(ms-…)@`
  git-URL form.
- Egress: verified 2026-09-06 from the fnos harvester container — direct
  connection to modelscope.cn works (no proxy, no DNS pollution);
  `use_proxy: false`.
- Tripwire: the `ms-` charset/length floor of 8 is from docs/third-party spans
  — measure the FIRST real valid key and re-align before narrowing; never add
  an upper bound (serpapi lesson).
- `inspect()` returns `[]` (hub token → user profile, no model enumeration).
- Push: `web/modelscope_push.py` `ModelScopePushService` (mirror of
  `web/agnes_ai_push.py`), env-gated by `MODELSCOPE_LOAD_BASE_URL` /
  `MODELSCOPE_LOAD_GROUP_ID` / `MODELSCOPE_LOAD_AUTH_KEY`. After a modelscope
  scan it POSTs validated keys to a gpt-load instance
  `POST {base}/api/keys/add-multiple` with body
  `{"group_id":<int>,"keys_text":"<key>\n<key>"}`. Defaults:
  `MODELSCOPE_LOAD_BASE_URL=http://192.168.1.18:43001` (the user's gpt-load
  instance; 192.168.1.18 is the fnos NAS LAN IP), `MODELSCOPE_LOAD_GROUP_ID=13`
  (group named "modelscope"), `MODELSCOPE_LOAD_AUTH_KEY=""` (empty → no
  Authorization header; non-empty → `Bearer <key>`); never raises; idempotent
  per run_id; writes one `push_logs` row (gpt_load_config_id=0, group_id=env
  int).
- Hook: `web/runner.py` `_on_completed` fires modelscope push iff
  `provider_name == "modelscope"` (daemon thread, ImportError-safe).
- Schedule: `("modelscope", "0 11 * * *", "examples/config-modelscope.yaml")`
  daily 11:00 in `web/scheduler.py`; seeds only into an EMPTY
  `schedule_config` table, so prod (fnos) needs a one-off row insert (or a UI
  add) after deploy.
- Redaction: NO `tools/patterns.py` entry (a bare `ms-` pattern would blitz
  logs); the provider never writes keys to disk/logs.
