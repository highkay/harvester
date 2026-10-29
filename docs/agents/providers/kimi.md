# kimi / kimi-ai / kimi-coding provider - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Feature: kimi-coding (corrected 2026-09-21)

- Endpoint `https://api.kimi.com/coding/v1`; auth-gated `GET /models` lists
  ONLY `kimi-for-coding, kimi-for-coding-highspeed, k3, k3-256k`.
  **`kimi-k3` is an Open-Platform (api.moonshot.cn) id and does NOT exist
  here** — the old `default_model: kimi-k3` made a live key answer 401 "model
  id does not exist", which was classified INVALID_KEY and silently discarded.
  Now `kimi-for-coding`; `provider/kimi.py` maps 401 model/plan errors
  (`model id does not exist|recognized as other|does not have access to`) to
  NO_MODEL so they land in wait-check instead.
- Error semantics (measured): 403 `access_terminated_error` "Your current
  subscription does not have access to Kimi Code right now" = authentic key
  with a lapsed/absent subscription -> NO_ACCESS -> `wait-check-keys.txt`
  (never pushed); 401 "The API Key appears to be invalid or may have expired"
  = dead/revoked key -> invalid. Real keys are `sk-kimi-` at 72 chars.
- The one key ever pushed (Aug) later 401'd on every probe — staleness law,
  not a misclassification.
- Pattern narrowed to `sk-kimi-[0-9A-Za-z_-]{20,}` (2026-09-21): the plain
  `sk-` pattern pulled ~300 dead platform/placeholder keys through the check
  stage every run for 0 valid keys; 16/16 real coding keys are `sk-kimi-` +
  72 chars, so only the prefixed form is extracted now. Tripwire: if a future
  run shows near-zero candidates at the check stage, widen back to `sk-` and
  re-measure before trusting the narrowing. **2026-09-26 CORRECTION — re-measured and REFUTED for kimi-coding: do NOT widen; the starvation is corpus composition, not the pattern (see the kimi-coding tripwire section).** 

## Feature: kimi + kimi-ai extraction anchored (2026-09-22)

- **Measured real-key corpus (n=245)**: 229/229 `kimi` (api.moonshot.cn) and
  15/15 `kimi-ai` (api.moonshot.ai) authentic no-quota keys (i.e. they passed
  provider auth) were exactly `sk-` + **48 alnum** (mixed case + digits) — same
  shape on both endpoints — plus one operator sample. Format alone therefore
  cannot separate Moonshot keys from other `sk-` vendors; only context can.
- **The flood**: both configs used a naked task-level
  `sk-[0-9A-Za-z_-]{20,}`, so every `sk-` run on a fetched page became a
  candidate — measured 1727 invalid (kimi) / 1001 invalid (kimi-ai) in one run,
  body lengths scattered 20-57 (placeholders like `sk-your…`/`sk-tes…`,
  `sk-ant-` keys, other vendors) against only 229/15 authentic keys.
- **Fix (mirrors agnes-ai)**: task-level extraction is anchored to
  Moonshot/Kimi env names —
  `(?i)(?:moonshot[_-]?(?:ai[_-]?)?api[_-]?key|kimi[_-]?(?:ai[_-]?)?api[_-]?key|moonshot[_-]?key)["'\]]{0,2}\s*[:=]\s*["']?(sk-(?!(?:ant|proj|svcacct)-)[A-Za-z0-9]{16,})["']?`
  — and every domain-named dork (`api.moonshot.cn` / `api.moonshot.ai` /
  `platform.kimi.ai` / `moonshot.ai` / `kimi.ai`) overrides with the wide
  `(?i)(?:Bearer\s+|["']\s*)?(sk-(?!(?:ant|proj|svcacct)-)[A-Za-z0-9]{16,})`
  form (the query itself is the anchor). No upper bound on the body: a fixed
  width silently drops keys if the provider rotates the format (serpapi
  lesson) — the 48 is documented, not enforced.
- Tests: `tests/test_kimi_pattern.py` (10) — env anchoring, bare-`sk-`
  rejection at every observed body length, single capture group, the
  ant/proj/svcacct lookahead, override-only-on-domain-dorks, no-upper-bound,
  and kimi↔kimi-ai lockstep. Both configs validate; suite 572 OK / 8 skipped.
- Tripwire: if a future run shows near-zero candidates reaching the check
  stage, widen back (drop the anchor) and re-measure before trusting it
  (2026-09-26: for kimi-coding the widening was measured and refuted — see that
  section; the anchor stays).
