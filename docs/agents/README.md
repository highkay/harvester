# docs/agents - harvester agent knowledge base

`AGENTS.md` (repo root) is the always-on core. This directory holds the full,
verbatim detail that used to inflate it to 216 KiB - past the harness limit of
64 KiB per context file, which silently truncated the file. Nothing was deleted:
every section below is byte-identical to its pre-split text (backup:
`.omo/tmp/AGENTS.md.pre-split-20261022`).

## How to use

- Read `AGENTS.md` first; it carries the cross-cutting rules and the map.
- Before working in an area, open the matching file from the table below.
- Add new area-specific knowledge here as a dated section; fix outdated facts in
  place and mark superseded claims explicitly (`SUPERSEDED: ...`).
- Promote a fact into `AGENTS.md` only when every session must know it.

## Index

| Area | Doc | Read before |
|---|---|---|
| Provider: github self-bootstrap | `docs/agents/providers/github.md` | touching `provider/github.py`, `web/self_bootstrap_push.py`, `examples/config-github.yaml` or GitHub-token scans |
| Search: hf backend | `docs/agents/search.md` | adding or debugging a non-GitHub search backend / `search/client.py` dispatch |
| Provider: serpapi | `docs/agents/providers/serpapi.md` | changing serpapi extraction (env-name + URL anchoring, no bare hex), validation (`account.json`) or push |
| Service: serpapi_proxy pool | `docs/agents/serpapi-proxy.md` | touching the standalone serpapi_proxy service or its API contract |
| Provider: groq | `docs/agents/providers/groq.md` | any groq scan: egress 403 history, `WGdyb3FY` decoys, socks5-vs-socks5h correction |
| Provider: agnes-ai | `docs/agents/providers/agnes-ai.md` | agnes-ai scans/pushes: socks5h DNS requirement, `/v1/models` presence-only trap, gpt-load group 19 |
| Provider: modelscope | `docs/agents/providers/modelscope.md` | modelscope scans/pushes: `users/me` validation, presence-only `/v1/models`, gpt-load group 13 |
| Provider: glm / glm-ai | `docs/agents/providers/glm.md` | glm work: measured `<32hex>.<16+>` format, probe-model policy (glm-4.5-flash), 429/1305 wait routing |
| Provider: kimi family | `docs/agents/providers/kimi.md` | kimi / kimi-ai / kimi-coding work: env-anchored `sk-` extraction, 48-char corpus, 401 model-id mapping |
| Provider: ollama | `docs/agents/providers/ollama.md` | ollama work: why 0 new keys, GitHub code-search semantics, `/v1/models` is public, gpt-load wedge |
| Provider: tavily (+ hikari push) | `docs/agents/providers/tavily.md` | tavily work: `/usage` 200 is not usable, per-exit bulk 429 blocks, wait-pool recovery, hikari target |
| Provider: brave | `docs/agents/providers/brave.md` | brave scans/validation/push or its config preset |
| Provider: exa | `docs/agents/providers/exa.md` | exa scans/validation/push, plan/quota handling or its config preset |
| Infra: egress routing | `docs/agents/egress.md` | changing `_pick_proxy`, `HARVESTER_PROXY*`, the egress pool, exit failover or compose proxy env |
| Audit: 2026-09-28 yield cull | `docs/agents/audits/2026-09-28-yield-cull.md` | deciding whether to enable/disable a provider (cull rationale) |
| Audit: 2026-09-28 lifecycle/disk | `docs/agents/audits/2026-09-28-evening.md` | changing restart/lifecycle reconciliation or disk retention bounds |
| Audit: 2026-09-30 fix sprint | `docs/agents/audits/2026-09-30-fix-sprint.md` | touching false-404 handling, lifecycle or elasticity |
| Audit: 2026-10-09 frequency | `docs/agents/audits/2026-10-09-frequency.md` | changing schedules/frequency policy or the fleet Fastly probe |

## Layout

- `providers/` - per-provider scan/validate/push knowledge and measured formats.
- `egress.md` - proxy selection, egress pool and exit failover.
- `search.md` - non-GitHub search backends.
- `serpapi-proxy.md` - the standalone key-pool service.
- `audits/` - dated production audits (yield, lifecycle, frequency policy).
