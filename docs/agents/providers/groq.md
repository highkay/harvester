# groq provider (egress + honeypots) - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Feature: groq provider — egress & honeypot facts (verified 2026-09-04)

> **2026-09-21 CORRECTION — the Cloudflare block described below is NO LONGER
> accurate.** Measured on fnos (container on host networking): the socks trio
> with `socks5://` (LOCAL DNS) returns the normal `401 invalid_api_key` (6/6
> probes); only `socks5h://` (remote DNS) still gets the bare 403. The
> dedicated relay `socks5://192.168.1.18:7890` is GONE (connection refused,
> nothing listening on the NAS). `HARVESTER_PROXY_GROQ` in fnos `.env` is now
> EMPTY so groq inherits the global trio. Re-verify before trusting either
> direction again.

- **Groq's Cloudflare edge 403-blocks fnos entirely.** Every request from
  fnos direct AND through the benchmarked socks trio
  (`192.168.1.18:1080/1090/1091`, whose egress is Cloudflare anycast
  `104.28.208.136`) gets bare `403 {"error":{"message":"Forbidden"}}` BEFORE
  key auth — even for a never-registered random key (normal clients get 401
  `invalid_api_key`). `provider/groq.py::_judge` maps 403+"forbidden" →
  INVALID_KEY, so a groq scan on fnos runs can NEVER produce a valid key,
  regardless of pool quality. This was the root cause of "0 valid since
  forever" (only 3 groq runs exist; schedule row added 2026-09-03).
- **Working egress**: `socks5://192.168.1.18:7890` (VPS relay, egress
  `107.172.141.203`) returns the normal 401 for invalid keys. Pin groq to it
  via `HARVESTER_PROXY_GROQ` in fnos `.env` — `web/runner.py::_pick_proxy`
  honors per-provider `HARVESTER_PROXY_<PROVIDER>` overrides (provider name
  upper-cased, punctuation → `_`), falling back to the global
  `HARVESTER_PROXY` rotation. The runner injects the picked proxy into the
  generated runtime YAML (comment in `_generate_temp_yaml` explains why).
- **Honeypot decoys**: repos poison the `"gsk_"` search space with fake keys
  whose bodies embed base64("XgroqX") == `WGdyb3FY` (measured: 44% of
  rejected candidates). `examples/config-groq.yaml` and the groq preset in
  `config/defaults.py` exclude them via a `(?!.*WGdy)` lookahead plus an
  alnum-only charset (drops GTK4 `gsk_*` symbols and doc placeholders).
  Length floor stays 20+ deliberately — the serpapi lesson: narrowing length
  without a measured real-key corpus silently drops every real key.
  Tripwires for future sessions: (1) "real keys are gsk_+48 alnum" is
  INFERRED from docs/decoys, not measured — measure the first real valid key
  and re-align only then; (2) if a hardened run yields near-zero candidates
  reaching the check stage, suspect the pattern first; (3) behaviour is
  pinned by tests/test_groq_pattern.py (decoy rejection, no capture groups,
  defaults↔examples lockstep).
- **"0 valid since forever" was ALSO a corpus-replay bug (fixed 2026-09-06).**
  `manager/task.py::_on_start` recovered the previous run's
  `links.txt`/`material.txt`/`invalid-keys.txt` (and shared `queue_state`
  files, provider-filtered) BEFORE backing them up, so every nightly groq
  run re-checked last night's pool instead of searching fresh — measured:
  consecutive links.txt sets were byte-identical (0 new/0 dropped, only
  row-order shuffle) while run_records said "completed". The egress/pattern
  fixes were necessary but unobservable under replay. Fix: `persistence.
  auto_restore` gate is now actually consumed by `_on_start` (it was a
  parsed-but-unused flag); `examples/config-groq.yaml` sets
  `auto_restore: false` (clean start each run, still backs up old files).
  Dorks widened too: `"gsk_"` plus `GROQ_API_KEY` env anchors,
  `api.groq.com` domain dorks, and a `created:>=2026-08-01` freshness window
  (max_pages 200); pattern excludes doc placeholders (marker words,
  abc123-style sequences, 4-char runs). Prod verification 2026-09-06:
  manager.log shows "clean start", 7 initial search tasks, fresh gather
  queue (5k+ links in 4 min), zero "Recovered" lines. Diagnosis recipe:
  set-diff two backups' links.txt + grep manager.log for
  "Recovered N unique links" vs "clean start". Fun fact: the 165-candidate
  pool that ran for 3h nightly was doc placeholders (gsk_xxx…/gsk_test…/
  gsk_abc123…) — all re-verified dead (163×401 + 2 network-EXH, 0 live).
- `run_records.total_keys_checked` is never written by `web/runner.py` (only
  `valid_keys_found`) — 0 there means "not wired", not "nothing checked".
