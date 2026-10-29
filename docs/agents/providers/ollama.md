# ollama cloud provider - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Feature: ollama cloud provider — why it produced 0 NEW keys (fixed 2026-09-22)

- **Symptom (measured)**: daily prod runs reported `valid_keys_found` 7–11 while
  `run_new_keys` held ZERO ollama rows from 2026-09-03 to 09-22 — the same keys
  were re-discovered and re-validated nightly. The provider itself is correct: all
  7 keys in `valid-keys.txt` answered 200 on `POST ollama.com/v1/chat/completions`,
  the 3 real-format keys in `invalid-keys.txt` answered 401, a random
  `<32hex>.<24 base64ish>` key answers 401. `GET /v1/models` and `/api/tags` are
  PUBLIC (200 without auth) — never use them to judge a key.
- **Root cause = the search side never reached the files that hold keys**:
  - `examples/config-ollama.yaml` carried the token dork
    `"ollama" "api_key" extension:env` (3,144 results). Its refine partitions are
    all `language:` variants of a file-type query and return 0
    (`... language:Python` = 0, `language:Text` = 0, `language:Shell` = 5), and the
    refine branch used to REPLACE pagination → only page 1 (100 links) was ever
    walked. Swapped to `"OLLAMA_API_KEY" extension:env` (407 results → pages 2..5
    walk the whole class; measured 24 pattern captures / 17 key-shaped on its page
    1 alone, 5 of them live and absent from the harvest).
  - `stage/definition.py::_handle_first_page_results` now emits page tasks for the
    original query *in addition to* refined tasks — every dork with >1000 results
    used to lose its tail (e.g. `"OLLAMA_API_KEY"` = 58,240 results).
  - `CheckStage` now routes retryable reasons (NETWORK_ERROR / TIMEOUT / 5xx /
    RATE_LIMITED) to `wait-check-keys.txt`; `search/client.py::chat` returns code 0
    (the sentinel the provider probe loops already use) on transport failure
    instead of 400, and `AIBaseProvider._judge` maps 0 → NETWORK_ERROR/TIMEOUT.
    Before this a TLS EOF (measured: 8/25 first probes, container-direct)
    permanently burned a live key into `invalid-keys.txt`.
- **GitHub code-search semantics (measured 2026-09-22 + docs)**: `sort`/`order` are
  inert — `sort=indexed&order=desc`, `order=asc` and no sort return identical
  ordering (30/30) because the docs mark both "closing down" and default to
  best match. Results are a stable relevance ranking: re-running a dork returns the
  same files (corpus plateau ≈38k links, +1.8k/run, dropped=0), and
  `created:`/`pushed:` do NOT exist in the legacy code-search qualifier set — so
  the "created:>=… freshness window" dork in `docs/agents/providers/groq.md` is a no-op, and freshness
  must come from dork/class diversity + cadence. The refine engine partitions only
  by the 28 `POPULAR_LANGUAGES` then 4 size buckets, so `YAML` (2,688), `JSON`
  (1,828), `Shell` (1,090), `Text`(.txt, 259) and `.env` classes stay unreachable
  unless the dork itself carries `extension:`/`filename:`.
- **Verification (bounded real run with the fixed config)**: 13 valid keys (10 not
  previously in the harvest) plus the expected `generated 9 page tasks` +
  `generated 27 refined tasks` pair for `"OLLAMA_API_KEY"` and `generated 4 page
  tasks` for the env dork. Leaked ollama keys are NOT in GitHub's
  supported-secret-scanning list (no push protection / partner revocation, unlike
  groq), so the supply is real and durable — 14/21 page-3 candidates were live.
- **Tripwires**: (1) do NOT add a bare `api[_-]?key=` branch to the ollama pattern —
  ollama keys share the `<32hex>.<body>` shape with glm/z.ai keys, so it floods the
  check stage with other vendors; (2) if a run reports 0 new keys while
  `valid_keys_found` stays flat, inspect `run_new_keys` and the corpus `.env` count
  before touching the provider.
- **Measured negatives (2026-09-22, don't re-litigate)**: (a) widening the pattern to
  `OLLAMA_CLOUD_API_KEY` / `OLLAMA_TOKEN` finds NO real keys — GitHub totals are
  `"OLLAMA_CLOUD_API_KEY"` = 3,704 and `"OLLAMA_TOKEN"` = 1,444, and their page-1
  fragments contain zero key-shaped strings (the widened alternation's extra captures
  were placeholders that 401); the key-bearing files are the `.env` ones the
  `extension:env` dork already covers. (b) `default_model: gpt-oss:20b` (no `-cloud`
  suffix) is correct — `/api/tags` lists it and live keys answer 200; the mock's
  `gpt-oss:20b-cloud` ids in `tests/test_ollama_provider.py` are just fixtures, and
  prod never produced a wait-check file (zero NO_MODEL verdicts). (c) The `-cloud`/
  suffix question and `GET /v1/models` presence-only behaviour are both moot for
  judging keys: only the chat probe discriminates.
- **Downstream visibility (where "扫不出有效键" shows up)**: `provider_group_mapping`
  maps `ollama → gpt_load_config_id=1, group_id=10` (group name `ollama`), so the
  generic `web/push.py` flow pushes each run's valid keys; `push_logs` for ollama read
  `keys_count=7-11, added_count=0, ignored_count=7-11` every night — i.e. only
  duplicates were ever pushed, which is exactly what an empty-looking pool looks like
  from the UI. Check `push_logs.added_count` after a fix like this one, not just
  `valid_keys_found`. Pool keys are re-verifiable at
  `GET {gpt_load}/api/keys?group_id=10` and by a chat probe that asserts
  `200 + "choices" + no "error"` in the body.
- **Catalog ≠ entitlement (2026-09-23, cost an operator a debugging round)**: the
  per-key model lists in `providers/ollama/summary.json` come from `GET /v1/models`,
  which is PUBLIC — it advertises the whole ollama.com catalog, paid models included.
  Free-tier harvested keys answer **402 `this model is not included in your free
  usage`** for `glm-5.1` / `deepseek-v4.1-flash` / `kimi-k3` / `qwen3.5:397b`, while
  `gpt-oss:20b`, `gpt-oss:120b` and `gemma4:31b` (plus the CLI alias `gpt-oss:20b-cloud`)
  answer 200 in 0.7–4 s. Quote the free list, not the catalog.
- **The gpt-load ollama group can wedge ("一直卡住")**: `/proxy/ollama/v1/...` hangs
  with `429 timed out waiting for a concurrent request slot` (300–1800 s waits; 1 of 14
  requests succeeded that day) although every upstream leg measures 1–2 s. Not egress:
  while wedged the group's own `socks5://127.0.0.1:1081` answered 200 in 1.5 s from the
  host, and switching the group to mihomo `7890` *or* to direct also hung — a
  `docker restart gpt-load` cleared it, after which the group served `gpt-oss:20b` in
  4.6 s and `gpt-oss:120b` in 1.0 s. Amplifiers in the fork's settings:
  `failover_status_codes=400-403,405-999` (a plan-level **402** triggers a 6-key retry
  spiral, so clients see a hang instead of a clean error) and `request_timeout=600 s`
  (a wedged slot stays wedged for 10 min). Client-side advice: use the free models, or
  add group `model_redirect_rules` mapping the paid ids onto `gpt-oss:20b`.
- **"Disabled" inventory (audited 2026-09-23, BOTH gpt-load instances)**: the only real
  off-switch is `schedule_config.enabled` — exactly one row, `cerebras`. Everything else is on:
  16 `provider_group_mapping` rows, 7 GitHub tokens, both `gpt_load_config` rows, and every
  scheduled provider's config file exists in the running image (incl.
  `examples/config-nvidia.yaml`, whose row was seeded 2026-09-22 20:23 and fires first at
  11:50). **Verified pool states (measured, not inferred)**: rn group 12 (`cerebras`, upstream
  `https://api.cerebras.ai`, test_model `gpt-oss-120b`) holds 28 keys all marked `active` but a
  serve probe answers **402 `payment_required` "Visit your billing tab"** in 0.8 s, and only
  1/28 was ever used (last use 2026-07-21) — gpt-load keeps them `active` because its validation
  hits a non-billing endpoint (the auth-valid/402 split AGENTS already records), so this pool is
  dead weight regardless of pruning. rn group 9 (`gemini2`, 306 keys = 263 invalid + 43 active)
  has **0 keys ever used** and its serve probe answers **404** (openai channel against
  `https://generativelanguage.googleapis.com` needs the `/v1beta/openai/...` base, so the group
  is misconfigured on top of having no producer). **Disabled by absence: `grok`** —
  `provider/grok.py` + `examples/config-grok.yaml` exist but there is no schedule row, no seed
  entry and no group mapping, so it never runs. **Off by flag AND omission: `gemini`** —
  registered (`provider/__init__.py:14`) and mapped (→ rn group 9), but there is no
  `examples/config-gemini.yaml`, no scheduler seed and no prod row, and the bundled preset in
  `config/defaults.py:317-319` sets `"enabled": False` (same shape as grok's, which
  `config/defaults.py:249-250` also disables; grok's standalone example config sets its task
  `enabled: true`, so a grok config CAN run once a schedule row exists). Consequence: both
  pools are un-replenishable — gemini's surviving 43 keys decay with nothing able to refill
  them, so either add config+schedule or retire the mapping/pool deliberately
  (cerebras: retire/park — its keys cannot infer even though 28 are "active").
  **Gemini invalid-marking audit (2026-09-23)**: a 10-key sample of the 263
  `invalid` entries re-checked with the deployed `GeminiProvider` through exit
  1090 gave 7 `invalid_key` + 3 `no_access`, 0 live — the marking is trustworthy,
  so the pool holds no recoverable keys; deletion was deliberately skipped (the
  group is parked and nothing consumes it — the open decision is retire vs
  unpark, not cleanup). Id/hash export for rollback:
  `/home/admin/harvester/rollback-gemini-invalid-keys.json`.
  **Tavily wait-pool recovery is running** (started 2026-09-23 13:53, ~1,967
  real entries at ≥5 s/key ≈ 3.5 h, alternate exits 1090/1091, resumable via
  `/tmp/tavily_wait_recovery_progress.txt`, terminal buckets appended to the
  result files and the recovered valid keys pushed immediately through
  `web/tavily_push.py`). Script: `.omo/evidence/tavily_wait_recovery.py`
  (copied to the container's `/tmp`; a container recreate wipes it — re-copy
  from the repo evidence dir).
  gpt-load has no disabled key status anywhere (fnos: 1177 active / 2 invalid, both in
  `qwenchina`; rn: gemini2 306 keys = 263 invalid + 43 active, cerebras 28 active, groq 5,
  mimosg 1 active + 1 invalid, agnes 40 — **266 invalid keys in total across both**), groups
  have no enabled column, `group_sub_groups` is empty, and
  `allowed_models='__disabled__'` (27 keys) means the per-key model allowlist is OFF (a
  group-10 key carrying it served 200; the only *restrictive* values are single-key
  allowlists `qwen-plus` / `glm-5.2`). Effectively-off pools are the EMPTY groups: `kimi`
  on both instances (fnos id 3 = 0 keys, rn id 23 = 0), `glm` (14) and `zai` (15) — the
  last two were cleared by the 2026-09-21 flash-only policy and refill only when a scan
  finds glm-5.3-flash-capable keys. `examples/config-full.yaml` carries 10 task-level
  `enabled: false` but is not used in production (all 20 rows point at per-provider files).
  A repo↔prod cron mismatch is cosmetic: the seed says `ollama 20 3 * * *`, the prod row
  (manual insert) says `0 3 * * *`.
- **Latent hole (FIXED 2026-09-23)**: `OpenAILikeProvider._judge`'s 200 branch
  used to treat a top-level `error` as a failure only when it was a **dict**; a
  200 body with a *string* `error` (native-Ollama shape) was accepted as
  success. A non-empty string `error` is now classified by the SAME markers
  (auth → INVALID_KEY, quota/billing → NO_QUOTA, else → BAD_REQUEST →
  wait-check), pinned by `tests/test_provider_status_maps.py` (string
  auth/quota/generic cases + empty-string-error stays success). No-`error` →
  success, non-JSON → UNKNOWN, and the deliberately loose success shape are
  unchanged. Every pooled key re-probed 2026-09-22 returned real completions,
  so the hole had not fired — still re-check the body shape whenever a
  provider starts returning soft errors with HTTP 200.
- **Bounded-run gotcha (cost an hour, 2026-09-22)**: `main.py` installs graceful
  SIGTERM handlers, so `subprocess.terminate()` does NOT stop a CLI scan — one ran
  67 min against the shared 10 req/min GitHub budget. Bound the wall clock
  externally (`taskkill /PID <verified> /T /F`) and never leave a decrypted prod
  token in a config a long-lived process holds. Pipeline logs are UTC; file mtimes
  are local (+8).
