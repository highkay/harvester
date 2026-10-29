# audit 2026-09-28 (yield cull + opencode) - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Ops: yield audit → provider cull + opencode launch (2026-09-28)

User-directed full audit ("verify everything live; deploy what works, disable
what doesn't"). 7-day `run_records`×`push_logs` evidence (links → valid →
net-new added):

- **DISABLED via `PUT /api/schedule/{p}` (hot reload, rows kept)**:
  github (1.03M links → 3 valid, ~344k links/valid — the self-bootstrap loop
  was the cluster's biggest search-quota consumer), kimi (341k→0), kimi-ai
  (171k→0), kimi-coding (290k→1), mimo-cn (246k→0), deepseek (691k→+3),
  qwen-intl (186k→+0), qwen-cn (326k→+10), mimo-sg (167k→+3). cerebras/groq
  were already disabled. Re-enable = same PUT with `enabled: true`.
- **KEPT (10 enabled)**: serpapi, nvidia, tavily, glm, glm-ai, modelscope,
  ollama, openrouter, agnes-ai + the new opencode.
- **agnes-ai push root cause + fix (d9012eb, faca500, 3c5686f)**: the rn
  gpt-load (`107.172.141.203:43001`) is a genuinely remote VPS — its group
  list is DISJOINT from the fnos instance (fnos ids 1-15+20, rn ids
  9/12/13/17/19/23; the old "same host" comment was wrong). The fnos→rn WAN
  path drops POST bodies larger than a few KB: gpt-load answers
  `HTTP 400 INVALID_JSON` wrapping its own `read tcp … i/o timeout` after
  ~60s (its body-read deadline). Size ladder measured: 1-3 keys ≈0.5s OK,
  10 keys ≈1.5s OK, 32-67 keys (2-5KB) fail 4/4. Fix: chunks of **10 keys**,
  180s timeout, and transport-flavoured 400s (markers `read tcp` /
  `i/o timeout` / `connection reset` / `unexpected EOF`) retry like network
  errors. mimo-sg's 09-08→09-21 "404" streak self-resolved (group 17 was
  created on rn between 09-21 and 09-28). **The 09-27 "lost" 67 agnes keys
  were never lost** — re-push after the fix returned added=0/ignored=67,
  `total_in_group=73`: they were already pooled by earlier pushes. Same trap
  applies to ANY future WAN-targeted push service (opencode defaults to the
  fnos LAN instance and stays at 100-key chunks).
- **opencode supply probe (2026-09-28, GitHub code-search total_count via a
  prod token)**: `"opencode.ai" "api_key"` ≈55k, `"opencode.ai/zen"` ≈42k,
  `"OPENCODE_API_KEY"` ≈33k, `extension:yaml` 586, `language:Python` 3.4k,
  `extension:env` 29. Supply is real; the domain dorks are the big wells.
  Baseline for judging tonight's 22:00 first run.
- **Review-fix the same day (`0c79712`)**: the `GPT_LOAD_AUTH_KEY` fallback
  now applies ONLY when the effective base URL is the default fnos instance —
  an overridden `OPENCODE_LOAD_BASE_URL` with an unset dedicated key would
  otherwise have leaked the fnos master key to a foreign host. Deployed by
  `docker compose cp` WITHOUT a restart on purpose: the only import of
  `web.opencode_push` is the lazy one inside `runner._on_completed`, and no
  opencode run had completed since the process started, so the 22:00 run
  picks the new file up fresh.
- **opencode session-header trap (`7611534`, found by live-probing the first
  run's wait bucket)**: the gateway 400s chat probes WITHOUT
  `x-opencode-session` before judging the key (`MissingSessionID` "cannot be
  routed efficiently") — garbage keys 401 fast, but every well-formed key
  lands in wait-check unjudged, making 0 valid structural. With the header
  the corpus resolves truthfully: dead → 401 `server_error` "Invalid
  credential" (a DIFFERENT body shape — no top-level `type:"error"` wrapper;
  the generic 401 fallthrough already maps it INVALID_KEY), authentic but
  unsubscribed → 403 "An active OpenCode Go subscription required"
  (NO_ACCESS → wait, never pushed — 6/16 of the first bucket were this
  class). The provider now sends a fresh `x-opencode-session` per probe;
  live-verified post-deploy: the re-run's invalid bucket holds exactly the
  keys my manual probes judged dead, its wait bucket exactly the
  subscription-lapsed ones. Manual corpus probes used
  `socks5://192.168.1.18:1090` — opencode.ai is reachable through the trio.
- **agnes push fix — service-level proof (2026-09-28 14:23)**: the fixed
  service pushed 67 keys in 6s with zero retries (`success|67|0|67`), vs
  4/4 failures over 255s before. The 10-key chunk + transport-400 retry
  combination is the correct WAN shape.
- **Cleanup sweep (2026-09-28)**: workstation worktrees `hv-final` /
  `hx-deploy2` removed (detached deploy-validation leftovers, both SHAs
  merged); fnos: 4 `fnos-custom` stashes dropped (all were
  Dockerfile.web/docker-compose.yml variants, superseded by rollback dirs),
  stray helpers (`fnos_poll.py`/`fnos_status.py`/`monitor_scan.py`) moved
  into `rollback-20260928-100300/`, old rollback dirs pruned to the latest
  two (`-20260926-230034`, `-20260928-100300`), and ~30 dead watcher/recovery
  files in `data/` removed (named list) or moved to `data/_attic/` (the
  sibling-session tavily recovery scripts, kept for replay). fnos repo root
  now carries only the two intentional live edits (`Dockerfile.web`,
  `docker-compose.yml`) plus `.env` backups and the gemini rollback export.
- **Cross-instance auth note (measured)**: the rn and fnos gpt-load instances
  accept the SAME master key (the rn row's decrypted key authenticated
  against the fnos instance) — rotate both together.
- **tavily master-key exposure scrubbed**: `/tmp/tavily_key_recovery.py`
  (hardcoded proxy master key fallback, 09-22) deleted from the container.
  Rotation still optional (the key itself never left the box).
- **links.db "disappearance" is NOT a regression**: shipped configs set
  `github_transport.index.skip_known_links: false` — the link index is
  configured OFF for dedup, so its absence from `data/` changes nothing.
  (2026-09-28 update: the index WRITE is now gated on the same flag, so
  `links.db` no longer grows when dedup is off — see `docs/agents/audits/2026-09-28-evening.md`.)
- **opencode provider LAUNCHED** (`b732e85` + `51d4dff`): endpoint
  live-verified (`GET /zen/go/v1/models` is PUBLIC 200 with 42 models incl.
  `glm-5.3` — presence-only trap, never used for validation; chat probe
  without a key → 401 `AuthError`). gpt-load group **20 `opencode`** created
  on the fnos instance (auto-id luckily landed on the code default),
  upstream `https://opencode.ai/zen/go`, test_model `glm-5.3`.
  `web/opencode_push.py` falls back to `GPT_LOAD_AUTH_KEY` when
  `OPENCODE_LOAD_AUTH_KEY` is unset — the fnos instance 401s no-auth
  management calls (measured). Seeded `0 22 * * *` (also inserted on prod,
  hot-reloaded; next run 2026-09-28 22:00). **Proof pending: tonight's first
  run** — watch `run_records` + `push_logs` (group 20 adds).
- **Deploy**: fnos @ `51d4dff` (rollback `rollback-20260928-100300`), whole
  tree cp + restart, md5 MATCH ×6, `/health` ok. Pre-restart salvage:
  serpapi +2 / qwen-cn +0 / tavily +0 (dupes); the 6 killed runs reconciled
  with duration + valid counts (serpapi had 542 valid at kill time).
