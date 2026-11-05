# Audit: 2026-11-05 ROI schedule re-tier

Every provider with run history was re-tiered by a measured unit — **net-new
keys per machine-hour**, not by intuition. Data: production `run_records`
joined to `run_new_keys`, last 8-12 runs per provider.

## The measurement

`keys per machine-hour = net-new hashes per run ÷ median run duration`

| tier | threshold | providers | measured k/h |
|---|---|---|---|
| A | >= 10 | tavily, nvidia, glm, agnes-ai, serpapi | 76.1 / 46.2 / 15.6 / 13.0 / 12.6 |
| B | 1-10 | glm-ai, openrouter, ollama, brave, exa, doubao | 7.7 / 7.5 / 3.4 / 2.4 / 2.0 / 1.8 |
| C | 0.02-1 | modelscope, qwen-cn, mimo-sg, qwen-intl, kimi-ai, kimi-coding, deepseek, kimi, github | 0.37 ... 0.02 |
| D | 0 | mimo-cn, mistral, gemini, siliconflow, opencode, clinepass, commandcode, groq, cerebras | 0 |

The distribution is the whole argument: **tier A is 5 providers producing 98.3%
of net-new keys while consuming 94 of 220 weekly machine-hours.** Tier C+D are
12 providers consuming 72 hours for ~1.6 keys.

## Cadence

- A: daily. B: Mon/Thu. C: weekly. D: weekly floor (house rule; a provider is
  never fully disabled, so D keeps one run per week even at zero measured yield).
- `github` moved from `50 */6 * * *` to weekly: 10.8 h per run at 0.02 k/h.
- `glm` moved from 3x/week to daily: it is the highest-yield Chinese provider
  and was under-scheduled relative to its measured rate.

## Concurrency check (the part that is easy to get wrong)

A 7-day simulation of every cron with the measured durations found **two
windows at 4 concurrent runs** (MAX_CONCURRENT_SCANS=3): Sunday 02:00 (deepseek
against nvidia+glm) and Sunday 05:00 (mistral against glm). Both were moved —
deepseek to Sunday 08:00, mistral to Sunday 20:00 — plus preventive moves for
gemini/siliconflow (to 21:00) and github (to Saturday 15:00, where its 10.8 h
run would otherwise triple-stack). Post-fix simulation: **peak 3, zero
over-capacity slots**.

Run the simulation before any future cadence change:

```
python - <<'PY'
# build events from the schedule table + median durations, bucket by hour, assert peak <= 3
PY
```

## Lockstep

`web/scheduler.py::_DEFAULT_SCHEDULES` and the pins in
`tests/test_web_scheduler.py` were updated in the same commit (`5e552dc`) so a
fresh database seeds the same cadence prod now runs. Seeds only fill an EMPTY
table; production was changed through `PUT /api/schedule`, hot-reloaded with no
restart.

## Rollback

`/home/admin/harvester/schedule-snapshot-20261105-pre-roi.json` holds the
pre-change table (29 rows, 8 enabled). `apply-schedule.py` in the evidence
directory replays the proposal idempotently (dry-run by default).

## Caveat that must survive into the next audit

Weekly expected keys in the proposal are a **linear extrapolation and therefore
an upper bound**. Measured evidence already shows short-interval reruns hit the
terminal-verdict cache and dedup (mimo-sg: 60 of 61 verdicts were cache hits in
one round), so raising frequency from weekly to daily will not scale the yield
linearly. Judge this re-tier by realized net-new per week after two cycles, not
by the projection.
