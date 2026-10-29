# hf search backend - moved from AGENTS.md (2026-10-22)

> Harvester agent knowledge. The always-on core is `AGENTS.md`; this file
> carries the full, verbatim detail for this area and is loaded on demand.
> The text below is unchanged from the pre-split `AGENTS.md`.

## Feature: hf search backend (search_type "hf")

- `search/hf.py` — unauthenticated HuggingFace Hub backend: dataset-name
  keyword search → recursive tree enumeration → text-ish file whitelist
  filter → raw `resolve/main/` URLs handed to the GATHER stage. Self-throttled
  (0.7 s/req), TTL discovery cache (600 s).
- Dispatch: `search/client.py` `search_with_count` routes `search_type == "hf"`
  before the GitHub path (GitHub behavior untouched). `config/schemas.py`
  `ALLOWED_SEARCH_TYPES` includes `"hf"`.
- `stage/definition.py` SearchStage: hf branches run BEFORE the GitHub
  credential gate (the source is anonymous).
- Preset: `examples/config-hf.yaml`; tests: `tests/test_search_hf.py`.
- Known limits: tree first page only (Link-header cursor not followed);
  whitelist = text suffixes + key-ish filename hints (binary junk filtered);
  dataset-name matching is noisy. Measured yield ≈ 0 real keys — keep as a
  low-frequency secondary source; do NOT invest further in it.
