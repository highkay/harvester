# Provider: gemini (generativelanguage.googleapis.com)

> Landing note written 2026-10-29 (magpie-provider-coverage todo 3).
> Evidence: `.omo/evidence/magpie-coverage/wave0-us.md` section (c).
> Provider code: `provider/gemini.py` (pre-existing, NOT modified by this landing).

## What it is

Google's Generative Language API. Endpoint
`https://generativelanguage.googleapis.com`, key in the `x-goog-api-key`
header. Validation is the lightweight models list gate
`GET /v1beta/models?pageSize=1`; keys come from Google AI Studio and are
shaped `AIza…`. Free tier exists but is rate-limited.

## The gate is real (anti-trap PASSED)

Measured 2026-10-29 from the fnos prod container (wave-0 US probe pack), two
socks exits plus direct:

| Probe | Result |
|---|---|
| `GET /v1beta/models?pageSize=1`, no key | 403 `Method doesn't allow unregistered callers …` |
| bad key (41-char form) in `?key=` | 400 `API_KEY_INVALID` |
| bad key (39-char form) in `?key=` (both exits) | 400 `API_KEY_INVALID` |
| bad key (39-char form) in `x-goog-api-key` header | 400 `API_KEY_INVALID` |

Four bad-key combinations, never a 200. The plan's BLOCKER condition (a bad key
getting 200) did not trigger, so the models list is **not** a public trap and
`provider/gemini.py`'s list-gate check is sound. A 200 body carrying a `models`
list is the valid verdict.

## Key shape: width 39, `AIza` prefix (candidate A kept)

Corpus sample 2026-10-29 (28 unique `AIza` values over six GitHub code-search
queries; GitHub code-search totals at that time: `AIza` ≈ 2.44M, `AIzaSy`
≈ 2.7M — approximate, order-of-magnitude only):

- width: 27 of 28 samples are **39** chars (`AIza` + 35); the one 38-char value
  is a truncated `.env` fragment;
- positions 5-6: `Sy` on **28/28** samples;
- charset: needs `-` (4 samples) and `_` (9 samples) in the body.

Two candidates were weighed:

| Candidate | Regex | Hits |
|---|---|---|
| A (chosen) | `AIza[0-9A-Za-z_-]{35}` | 27/28 |
| B | `AIzaSy[A-Za-z0-9_-]{33}` | 27/28 |

They match the same set, so B buys no measurable precision. Per the plan's rule
("pick B only if A over-matches"), the landing keeps **A** — which is the
`config/defaults.py` gemini preset verbatim. Zero diff to `config/defaults.py`,
zero capture groups (naked prefix pattern), lockstep pinned by
`tests/test_gemini_pattern.py`.

Limitation: the corpus is mostly Firebase / Google client keys, so the shape
confirms the token form only — it says nothing about whether that project has
the Generative Language API enabled. Verdicts come from 400 `API_KEY_INVALID`
vs 200 models list.

## Known behaviour (pinned as shipped, not changed here)

`provider/gemini.py` is out of scope for this landing, so three classifications
are recorded as risks rather than fixed:

- **403 carrying `PERMISSION_DENIED` → `INVALID_KEY`**. Google attaches that
  status to its "unregistered callers" 403 too, so a proxy/edge 403 of that
  shape would permanently discard the candidate. With a real token the check
  path never reaches this shape (empty tokens are rejected client-side).

- **429 with `RESOURCE_EXHAUSTED` → `NO_QUOTA`** (the no-quota bucket), not
  `RATE_LIMITED`. A free-tier per-minute limit therefore files the key as quota
  instead of retryable; the key is not discarded as invalid. A 429 without the
  quota markers does fall through to `RATE_LIMITED` (retryable).
- **An unparseable 200 body → `UNKNOWN`**. CheckStage files `UNKNOWN` in
  `invalid-keys.txt`, i.e. a permanent discard; a proxy interstitial served
  with 200 would burn a live key. The openai_like family already fixed this to
  `SERVER_ERROR` (empty body) — gemini has not, and is a candidate follow-up.

`tests/test_gemini_provider.py` pins all three behaviours with a note to update
the test together with any fix.

## Ops

- Config: `examples/config-gemini.yaml` (use_proxy: true, 0.2 req/s, burst 2,
  max_links_per_run 9000, search_types [code], env anchor `GEMINI_API_KEY`,
  domain anchor `generativelanguage.googleapis.com`).
- Schedule seed: weekly `0 5 * * fri` (Friday 05:00 sits before the 06:00
  serpapi window and after Thursday's openrouter job).
- `default_model` mirrors the defaults preset (`gemini-3.5-flash`); the check
  path never sends a model id, so it only matters to `inspect()`.
- Corpus is the largest of the wave-1 additions (millions of hits), so the
  9000-link per-run bound is what keeps a run near the productive head.
