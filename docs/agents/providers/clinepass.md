# Provider: clinepass (api.cline.bot)

> Landing note written 2026-10-29 (magpie-provider-coverage todo 10).
> Evidence: `.omo/evidence/magpie-coverage/wave0-cn.md` section (e).

## What it is

Cline's paid key service (ClinePass). OpenAI-compatible endpoint at
`https://api.cline.bot/api/v1`, Bearer auth, keys issued from
`app.cline.bot` Settings > API Keys (env names `CLINEPASS_API_KEY` /
`CLINE_API_KEY` per the official docs).

## The trap: `/api/v1/models` is PUBLIC

Measured 2026-10-29 from the fnos prod container (wave-0 CN probe pack):

| Probe | Result |
|---|---|
| `GET /api/v1/models` no key | 200, 41,064-byte catalog |
| same, bad key | 200, byte-identical |
| same, via `socks5h://192.168.1.18:1090` and `:1091` | 200, byte-identical |
| `POST /api/v1/chat/completions` bad key, `max_tokens=1` | **401** `{"error":"Unauthorized: Please make sure you're using the latest version of Cline and re-authenticate your Cline account."}` |
| same, no key | 401, identical body |

So a models-200 proves NOTHING about a key. The judge in
`provider/clinepass.py` never consults the models list; it posts a minimal
chat completion and maps 401 → `INVALID_KEY`. Note the 401 body carries
`error` as a plain STRING, not an object.

First direct probe threw `SSLEOFError`; three retries (direct + both socks
exits) all answered 200, judged transient TLS cut, not blocking.

## Probe model id source

`GET /api/v1/ai/cline/recommended-models` is a PUBLIC catalog (200, no
auth). Fetched 2026-10-29; the `free` family lists `cline-free/*` ids and
the `clinePass` family lists `cline-pass/*` ids. The provider's
`default_model` is `cline-free/solar-mini4` (a text model — the other
cline-free id at fetch time, `cline-free/nemotron-3.5-super-vl`, is
vision-language and a text-only probe might fail on it). Because the
catalog is public, the id is only a probe target, never a validity signal.
If the id disappears upstream the probe answers 404 → `NO_MODEL` →
wait-check (not invalid).

## Key shape: undetermined

Official docs (`https://docs.cline.bot/getting-started/clinepass`, 200 on
2026-10-29) contain zero `sk-` hits — keys have NO `sk-` prefix. GitHub
code-search sampling (4 queries × 10 rows, `CLINEPASS` total 11,408) found
no real key literal — only env names, file paths and a `YOUR…` placeholder.
Extraction is therefore env-anchored (`CLINEPASS_API_KEY` / `CLINE_API_KEY`)
plus domain-anchored (`api.cline.bot`) with a wide body class
`[A-Za-z0-9_\-]{20,}` and a `your_` placeholder guard. Refine only after a
pilot run shows the real shape.

## Ops

- Config: `examples/config-clinepass.yaml` (use_proxy: true, 0.2 req/s,
  max_links_per_run 9000).
- Schedule seed: weekly `0 23 * * sat` (after mimo-sg 15:00 and opencode
  18:00).
- Subscription-service keys (magpie counts it as a relay user asked to
  keep); corpus 11,408 on 2026-10-29, expect low churn.
