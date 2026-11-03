# Provider: qwen family (DashScope / Bailian)

Qwen uses the OpenAI-compatible DashScope endpoints. Task names, result
directories and push mappings are `qwen-cn` and `qwen-intl`, not the provider
type `qwen`.

## Endpoints and validation

- CN: `https://dashscope.aliyuncs.com/compatible-mode/v1`, `use_proxy: false`.
- International: `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`,
  `use_proxy: true`. Keys are region-specific.
- `GET /models` is the authentication gate (401/403 means invalid), but a 200
  does not prove inference capability or balance. A minimal chat completion
  therefore runs after it.
- HTTP 400 with `Arrearage` / `out_of_service` / `good standing` is NO_QUOTA.
  Plain 429 throttling is retryable, not terminal quota exhaustion.
- Default completion and flagship probe model: `qwen3.8-max-0902`.

## Capability tiers (2026-11-02)

`qwen-cn` and `qwen-intl` declare the same tiers:

| Tier | Probe model |
|---|---|
| `flagship` | `qwen3.8-max-0902` |
| `legacy` | `qwen-turbo` |
| `thirdparty` | `deepseek-v4.1-flash-1017` |

The third-party id comes from the Bailian text-generation model list fetched
2026-11-02. The models gate and completion gate run before tier probing, then
every tier gets its own `max_tokens=1` completion probe.

Only OK tiers write `valid-keys-<tier>.txt`; the union `valid-keys.txt` keeps
every valid key. A RETRYABLE tier does not cancel another tier's OK result.
If every tier is temporarily indeterminate, the key is recoverable
(wait-check), not permanently invalid.

## Push mappings on fnos

- `qwen-cn` -> group 6; `qwen-cn:flagship` -> group 29;
  `qwen-cn:legacy` -> group 30; `qwen-cn:thirdparty` -> group 31.
- `qwen-intl` -> group 7 only. It intentionally uses the single-group union
  fallback until tier mappings are added; its tier files are still written.
- With tier files present, push fans out to tier groups and does not also
  push the union. A legacy run with only the union file still uses the base
  group when every tier file is empty or absent.
- Mapping keys use the task name (`qwen-cn:*`), never `qwen:*`.

## Production measurement and supply state (2026-11-02)

Three CN candidates from recent backups and one international candidate were
re-probed. CN flagship answers were 400 `Arrearage`, legacy was 429
rate-limited and third-party was NO_ACCESS; the international key answered
401 on every tier. No live qwen key was available for a positive tier-split
demonstration. The `qwen3.8-max-0902` id itself was accepted far enough to
return the account's Arrearage body, so these results do not indicate a bad
probe id. Positive qwen tier routing is pinned by unit tests; doubao supplied
the live seed-only boundary proof.

Evidence: `.omo/evidence/capability-20261030/prod-measure.md`.

## Terminal-verdict cache and measured yield (2026-11-03)

`qwen-cn` and `qwen-intl` are inside `VERDICT_CACHE_TASKS`
(`stage/definition.py`). An explicit `INVALID_KEY` is reused for 30 days and an
explicit `NO_QUOTA` for 14 days; every other verdict is re-probed. The cache
key is `SHA-256(task + key + normalized base_url + endpoint + probe_model +
tier)`, so a region/endpoint/model/tier change is a miss, never a false hit.
`run-metrics` reports `live_probes` and `cache_hits` separately and their sum
equals `checked`.

Measured production yield through 2026-11-03:

| Task | Runs | Lifetime valid | First run | Last run |
|---|---|---|---|---|
| `qwen-cn` | 71 | 838 | 2026-08-13 | 2026-11-03 |
| `qwen-intl` | 11 | 12 | 2026-09-21 | 2026-11-02 |

The 2026-11-03 user-directed efficient pass (`qwen-cn` run
`d6599132-ddba-48a7-850d-712362aeb3f9`, 1236s, 241 checked, 20,816 links,
300 materials) returned 0 valid keys. That run is a manual efficiency probe,
not a natural cycle, so it is not a yield baseline.
