# siliconflow provider

Landed 2026-10-29 (magpie-provider-coverage wave 1, todo 4). Plan:
`.omo/plans/magpie-provider-coverage.md`; evidence:
`.omo/evidence/magpie-coverage/wave0-cn.md` (§a) and
`.omo/evidence/magpie-coverage/todo4-siliconflow.md`.

## Facts measured from the production container (2026-10-29)

All probes ran inside `harvester-web` on fnos; the corpus sampling used GitHub
code search with `Accept: application/vnd.github.text-match+json` through
`socks5h://192.168.1.18:1090`. Keys were masked (first 4 + last 2) before
printing; nothing was written to production state.

| Probe | HTTP | Observation |
|---|---|---|
| `https://docs.siliconflow.cn/` | 200 → 302 `/docs/userguide/introduction` | fumadocs/Next.js HTML shell, 134,148 B |
| `https://cloud.siliconflow.cn/models` | 200 (login wall) | redirects to `account.siliconflow.cn/zh/login?redirect=...models` |
| `https://cloud.siliconflow.cn/api/v1/models?type=text&sub_type=chat` | 200 (login wall) | 2,971,612 B of login-page HTML, no JSON |
| `https://docs.siliconflow.cn/docs/userguide/faqs/rate-limit-and-upgradation` | 200 | server-rendered body (survives `<script>` stripping) |
| `GET https://api.siliconflow.cn/v1/models` (no key) | **401** | `{"code":30014,"data":null,"message":"Token is invalid."}` |
| same (bogus `sk-0000…`) | **401** | byte-identical to the no-key answer |

The FAQ page's server-rendered text states, verbatim:
「实名认证 后使用全部的免费模型。」 and「**免费模型调用免费**，账户的 费用账单
中看到此类模型的费用为调用消耗是 0。」 So a free tier exists; it is not just a
JS-shell claim.

**Free-model id list: NOT obtained.** Per-model ids live only in the
「模型广场」 page, which is login-walled (a2/a3 above); `llms.txt` and
`sitemap.xml` are both 404. `docs/api/models-get` documents only the authed
"list my models" call.

## Key shape measured 2026-10-29 (GitHub code search, masked)

| Query | total_count | What the sampled fragments carried |
|---|---|---|
| `"SILICONFLOW_API_KEY"` | 25,568 | only placeholders (`your-siliconflow-api-key`, `sk-your-key-here`) |
| `"SILICONFLOW_API_KEY" sk-` | 5,128 | 48-char real bodies + `sk-your-key-here` placeholders |
| `"SILICONFLOW_API_KEY=" sk-` | 2,068 | placeholders only |
| `"api.siliconflow.cn"` | 50,816 | real 48-char literals inside yaml/python config blocks |
| `"api.siliconflow.cn" sk-` | 12,128 | real 48-char literals |
| `"siliconflow" "sk-"` | 38,016 | real 48-char literals |
| `"siliconflow" "Bearer"` | 31,808 | real 48-char literals |
| `"siliconflow" "Authorization: Bearer"` | 6,400 | one real 48-char literal |
| `"api.siliconflow.cn" "Bearer" sk-` | 4,912 | no key-shaped literals |

Shape histogram across three sampling passes (per_page 30/50/100): **every
alnum-only non-placeholder hit at or above the 32-char floor had a 48-char
body** — nearly all lowercase-only (e.g. one literal in `Zacarx/AICodeScan`'s
yaml config, right next to `https://api.siliconflow.cn/v1/chat/completions`),
one with digits and uppercase (`sk-FastAPI1YzE…xW`). The only literal below the
floor was a 20-char dummy (`sk-kk…kk`); hyphenated placeholders
(`sk-your-key-here`, `your-siliconflow-api-key`) never satisfy the alnum body.
The 48-char figure is documented, not enforced: the pattern has no upper bound.

Pattern decision: env-anchored at task level on
`siliconflow[_-]?(api[_-]?)?key`, domain dorks (`api.siliconflow.cn`,
`"siliconflow" "sk-"`, `extension:env|yaml|json`) widen to a Bearer/quoted
variadic. Body floor `{32,}` (below the measured 48 so a rotated shorter body is
still caught), **no upper bound** (serpapi fixed-width lesson), alnum-only body,
placeholder lookahead `(?!your|test|dummy|example|placeholder|changeme|xxx|fake|sample)`.
`config/defaults.py` and `examples/config-siliconflow.yaml` are lockstep-pinned
by `tests/test_siliconflow_pattern.py` (brave `_preset_task` mechanism).

## Judge taxonomy — documented, not measured with a live key

`https://docs.siliconflow.cn/docs/userguide/faqs/error-code` was fetched on
2026-10-29. **No real key was available**, so the 402/403/429 bodies below are
the documentation's shapes, not wire samples; only the 401 was measured (above).
`tests/test_siliconflow_provider.py` pins the mapping with fixtures built from
the docs.

- **401** → `INVALID_KEY` (status first; the measured body is
  `{"code":30014,…,"message":"Token is invalid."}`).
- **402** ("Account in arrears") → `NO_QUOTA`.
- **403** ("Insufficient permissions. The most common reason is that the model
  requires real-name authentication") → `NO_ACCESS` (wait-check). A 403 body
  that itself rejects the key (`Token is invalid` etc.) → `INVALID_KEY`.
- **429** (`RPM / RPD / TPM / TPD / IPM / IPD`) → `RATE_LIMITED`, i.e.
  recoverable. A 429 whose body carries a balance/arrears marker
  (`insufficient balance/quota`, `余额不足`, `欠费`, `请充值`, `recharge`) →
  `NO_QUOTA`. This split is why `provider/siliconflow.py` does NOT map 429 to
  no-quota wholesale (qwen's `Throttling.RateQuota` lesson).
- **404**, and **400 with code `20012` ("Model does not exist")** → `NO_MODEL` →
  wait-check. The 404 branch is defensive: the docs list only the 400/20012
  shape for a missing model.
- Unparseable body (captive HTML, truncated proxy page) → `SERVER_ERROR`
  (retryable). Never `UNKNOWN`, which CheckStage would file as a permanent
  invalid discard.
- 503/504 → `SERVER_ERROR` via the base judge (docs: high load, retry later).

## Probe model — UNVERIFIED, must be confirmed by the pilot run

`default_model` is `Qwen/Qwen2.5-7B-Instruct`. Wave-0 could not read the
free-model id list (login wall), and no live key was available at landing time,
so this id was chosen without a wire measurement. **The pilot run (todo 21) must
verify it against a real key's `/v1/models` catalog** and update
`provider/siliconflow.py`, `config/defaults.py` and
`examples/config-siliconflow.yaml` together if it is wrong. A wrong id is safe:
404 / 400 code 20012 route to `NO_MODEL` (wait-check), so keys are re-checked
rather than discarded.

## Config / egress / schedule

- `examples/config-siliconflow.yaml`: `use_proxy: false` (domestic endpoint,
  direct connect), `rate_limit 0.2/2`, `global.max_links_per_run: 9000`,
  `search_types: [code]`, 11 dorks. `provider_type: siliconflow`.
- `config/defaults.py` preset ships `enabled: False`; the example is
  `enabled: true`.
- Scheduler seed: `("siliconflow", "0 5 * * sat", "examples/config-siliconflow.yaml")`
  in `web/scheduler.py::_DEFAULT_SCHEDULES` — weekly floor, Saturday 05:00 sits
  before mimo-sg 15:00 and opencode 18:00 on the same day.
- Push: no `provider_group_mapping` row yet; the gpt-load group is wired in
  todo 20. Until then a valid key only lands in
  `data-siliconflow/providers/siliconflow/valid-keys.txt`.

## Tests

`tests/test_siliconflow_pattern.py` (8 tests) — lockstep, measured shape,
placeholder/short-body rejection, no upper bound, single capture group.
`tests/test_siliconflow_provider.py` (24 tests) — registration, the full judge
taxonomy above, probe shape (Bearer header, default model, `use_proxy: false`),
`inspect()` model listing, and the empty-token no-request path.

Run: `F:\git\harvester\.venv\Scripts\python.exe -m unittest
tests.test_siliconflow_pattern tests.test_siliconflow_provider` from this
worktree → 32 tests OK (2026-10-29). `uvx ruff check` on the three new Python
files → clean. `main.py -c examples/config-siliconflow.yaml --validate` → exit 0
when `GITHUB_TOKENS` carries any non-`your_` placeholder (house convention: the
loader drops the YAML placeholder and then requires an env token).
