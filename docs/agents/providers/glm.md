# glm / glm-ai providers - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Feature: glm + glm-ai providers (split + first yield run, 2026-09-21)

- **Key format is MEASURED, not inferred**: `<32 lowercase hex>.<16+ mixed-case
  alnum>`, 49 chars (sample live-validated at 200 on api.z.ai). The original
  pattern `[0-9a-f]{32}\.[0-9a-f]{32}` (32-hex secret) matched NO real key —
  scans looked healthy while extracting 0 candidates (serpapi lesson again).
  Fixed to `[0-9a-f]{32}\.[A-Za-z0-9]{16,}` in `examples/config-glm*.yaml`,
  `config/defaults.py`, `examples/config-full.yaml` and `tools/patterns.py`
  (the redaction pattern had the same bug and leaked real keys into logs).
- **Endpoints**: `glm` -> `https://open.bigmodel.cn/api/paas/v4` (direct);
  `glm-ai` -> `https://api.z.ai/api/paas/v4` (see `docs/agents/egress.md`). Both
  expose an auth-gated `GET /models` (the old "no /models endpoint" comment was
  wrong) but validation uses the chat-completions probe with `glm-4.5-flash`.
  **Policy 2026-09-24 (REVERSES 2026-09-21): the probe model is
  `glm-4.5-flash`** — the 5.3-flash-only policy classified every free-tier key
  as no-quota (429/1113) and produced 0 valid forever; measured on prod
  2026-09-24 the no-quota bucket keys answered 200 for glm-4.5-flash (3/3
  bigmodel.cn + 2/2 z.ai), and Zhipu's pricing page confirms glm-5.3-flash is
  a PAID model while glm-4.5-flash is in the free tier. The gpt-load `glm`
  (group 14) and `zai` (group 15) `test_model`s were moved to glm-4.5-flash
  the same day via `PUT /api/groups/{id}`, and the no-quota backlog was
  salvaged + re-pushed (`.omo/evidence/ulw-20260924-zero-yield.md`). History:
  the 2026-09-21 policy chose 5.3-flash because `glm-4.5-flash` answered 200 on
  10/10 CN probes but `glm-4.7-flash` was mostly 429 code 1305 and non-flash
  ids returned 1113; 362 free-tier keys were cleared from both groups then.
- **Status map**: 401 / body code 1000·1001·1003 -> invalid; 400 code 1211 ->
  NO_MODEL; 402 / code 1113 -> no-quota; **429 code 1305 (model overload) ->
  RATE_LIMITED -> `wait-check-keys.txt`** (62 occurrences in one 45-min window
  measured — expect a fat wait bucket on busy days; those keys are recoverable,
  not lost).
- **First production run (manual, 3h18m, ended by a service restart)**: `glm`
  234 valid / 164 invalid / 100 wait (60.8k links); `glm-ai` 25 valid / 38
  invalid / 27 wait (41.8k links). Pushed: `glm` -> gpt-load group 14
  (234/234/0), `glm-ai` -> group 15 (25/25/0, union of a `backup-<ts>/` dir +
  live file). `run_records.valid_keys_found` was NOT written (the run was
  reconciled as `failed: interrupted by service restart`) — authoritative
  counts live in the provider files + `push_logs`.
- **Config**: `auto_restore: false` in both (the first-run pool is ~60k links;
  replaying it daily would re-gather the whole backlog). Schedules: `glm`
  `0 12`, `glm-ai` `0 13` (daily, staggered).
