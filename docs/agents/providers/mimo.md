# Provider: mimo (Xiaomi MiMo)

MiMo has three separate credential surfaces. They are different products, not
regions of one endpoint:

| Surface | Base URL | Key prefix | Tasks |
|---|---|---|---|
| Token Plan CN | `https://token-plan-cn.xiaomimimo.com/v1` | `tp-` | `mimo-cn` |
| Token Plan SG | `https://token-plan-sgp.xiaomimimo.com/v1` | `tp-` | `mimo-sg` |
| Pay-as-you-go | `https://api.xiaomimimo.com/v1` | `sk-` | none (coverage gap) |

A Token Plan key is region-bound: a CN key answers 401 on the SG cluster and
vice versa. Pay-as-you-go `sk-` keys cannot be validated on either Token Plan
endpoint, which is why only `tp-` keys are collected today. `sk-`
pay-as-you-go keys are still unscanned — a known, deliberately unmeasured gap.

## Validation: the `/models` gate is not a quota gate

`GET /models` returns 200 for any authentic Token Plan key, including
zero-balance accounts. `provider/mimo.py::check` therefore runs the models gate
and then a minimal chat completion (`max_tokens=1`, `mimo-v2.6-pro`), so a
zero-balance key lands in `no-quota-keys.txt` instead of being called valid.

Quota markers (case-insensitive): `exceeded_current_quota`, `insufficient
quota`, `quota exhausted`, `quota exceeded`, `insufficient
balance/credits/funds`, `credit balance`, `billing not active`, `arrearage`,
`out_of_service`, `good standing`. A bare `quota` is deliberately NOT a
marker: `Throttling.RateQuota` is a refillable rate limit on a funded account
and must stay retryable. This is pinned by `tests/test_mimo_provider.py`.

## Key shape and corpus

- Token Plan keys are `tp-` + 48 alphanumerics. Extraction is anchored to the
  env names (`MIMO_*` / `XIAOMIMIMO_*`) and the endpoint hosts, with a
  placeholder lookahead, so bare `tp-` runs on a fetched page never become
  candidates.
- `global.max_links_per_run: 9000` (set in the 2026-10-24 wave). Measured
  discovery-to-queue ratio is about 3.3x, so the cap lands near 30k discovered
  links. The cap is charged on UNIQUE queued links, not on duplicate
  discoveries.
- `auto_restore: false`: a recurring schedule must start clean or the previous
  run's backlog is replayed and the corpus ratchets.

## Push mappings on fnos

- `mimo-cn` -> gpt-load group 5 (`fnos-gpt-load-group-5`).
- `mimo-sg` -> gpt-load group 4 (`mimosg`).

## Terminal-verdict cache (2026-11-03)

`mimo-cn` and `mimo-sg` are inside `VERDICT_CACHE_TASKS`
(`stage/definition.py`), so an explicit `INVALID_KEY` is reused for 30 days and
an explicit `NO_QUOTA` for 14 days. Everything else is re-probed. The cache key
is `SHA-256(task + key + normalized base_url + endpoint + probe_model + tier)`,
so a region, endpoint, model or tier change is a miss rather than a false hit;
raw keys are never written to disk. Hit counts appear as `cache_hits` in
`run-metrics`, and `live_probes + cache_hits == checked`.

## Measured production yield (through 2026-11-03)

| Task | Runs | Lifetime valid | First run | Last run |
|---|---|---|---|---|
| `mimo-cn` | 57 | 186 | 2026-08-13 | 2026-11-03 |
| `mimo-sg` | 14 | 12 | 2026-09-21 | 2026-11-02 |

The supply is thin but not zero: `mimo-cn` yields a handful of keys per
productive run and `mimo-sg` is close to exhausted. The 2026-11-03
user-directed efficient pass (`mimo-cn` run `368d06d1-f353-4f8f-8185-d8458c110500`,
1681s, 82 checked, 23,356 links, 119 materials) produced 0 valid keys; that run
is a manual efficiency probe, not a natural weekly cycle, and is not used as a
yield baseline.

## References

- `.omo/evidence/mimo-qwen-doubao-yield-optimization-execution/task-10-mimo-ttp-canary.json`
- `.omo/evidence/mimo-qwen-doubao-yield-optimization-execution/task-12-natural-runs.json`
- `tests/test_mimo_provider.py`, `tests/test_mimo_pattern.py`
