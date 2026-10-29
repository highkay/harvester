# serpapi provider - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Feature: serpapi provider (scan + validate + push)

- `provider/serpapi.py` — `SerpapiProvider` (registered as `"serpapi"`, mirror
  of `provider/tavily.py`). Validates via SerpApi Account API
  (`account.json?api_key=`); 401 → INVALID_KEY, 200 with account JSON →
  success (bare 200 without account fields → UNKNOWN, guarding against the
  search.json-style open endpoint). Keys are prefix-less 64-char hex — MEASURED on prod 2026-08-29: 395/395 valid keys are exactly 64 hex chars. The harness accepts 20-64 chars so validation stays length-independent; a fixed-32 pattern would truncate every real key (scans looked healthy, yielded nothing).
- Extraction (in `examples/config-serpapi.yaml` + `config/defaults.py`
  preset): context-anchored — env-name assignments (`SERPAPI_API_KEY` /
  `SERPAPI_KEY` / `SERP_API_KEY` / `serpapi[_-]?...key`) at task level, plus a
  domain-anchored variadic that adds query-string URLs
  (`?api_key=` / `&api_key=` / `&amp;api_key=`, any position in the URL).
  NO bare `[0-9a-f]{32}` AND no bare `api_key` branch — the first production
  scan proved a naked `api[_-]?key` branch floods the check stage (~84
  candidates/link, 55k total); env-name + URL forms only.
- Push: `web/serpapi_push.py` `SerpapiPushService` (mirror of
  `web/tavily_push.py`), env-gated by `SERPAPI_PROXY_BASE_URL` /
  `SERPAPI_PROXY_AUTH_KEY` — silent no-op until configured. Prod (fnos)
  configures them now: `SERPAPI_PROXY_BASE_URL=http://192.168.1.18:48081` +
  pool master key (see `docs/agents/serpapi-proxy.md`). **2026-10-15 SUPERSEDED: the target is
  now tavily-hikari `http://192.168.1.18:8787` (see `docs/agents/providers/tavily.md`
  below); 48081 no longer answers (measured 2026-10-19: curl exit 000).**
  Contract identical to
  TavilyProxyManager: `POST {base}/api/keys`, Bearer, `{"key","alias":...}`;
  only 20–64-hex keys are pushed; one `push_logs` row.
- Hook: `web/runner.py` `_on_completed` fires serpapi push iff
  `provider_name == "serpapi"` (daemon thread, ImportError-safe).
- Schedule: `("serpapi", "10 */6 * * *", "examples/config-serpapi.yaml")` in
  `web/scheduler.py` — seeds only into an EMPTY `schedule_config` table, so
  prod needs a one-off row insert (or UI add) after deploy.
- Redaction: NO `tools/patterns.py` entry (bare hex would blitz logs); the
  provider never writes keys to disk/logs.
- Tests: `tests/test_serpapi_provider.py`, `tests/test_web_serpapi_push.py`,
  `tests/test_web_runner_serpapi.py`; scheduler expectations updated in
  `tests/test_web_scheduler.py`.
