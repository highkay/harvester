#!/usr/bin/env python3

"""
PipelineRunner — thread-bridge between the web layer and the blocking
CLI HarvesterApp pipeline.

Wraps ``main.HarvesterApp`` in background threads (ThreadPoolExecutor)
so the FastAPI event loop stays responsive.  Tracks run state in the
``run_records`` SQLite table and fires a completion hook that T6's
push module can consume.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from tools.logger import get_logger

from .crypto import decrypt_str

logger = get_logger("web.runner")

# ---------------------------------------------------------------------------
# Workspace helper
# ---------------------------------------------------------------------------


def _load_workspace() -> Path:
    """Return the workspace directory path.

    Reads ``HARVESTER_WORKSPACE`` env var; defaults to ``./data``.
    """
    return Path(os.environ.get("HARVESTER_WORKSPACE", "./data")).resolve()


def _get_db_path() -> str:
    """Resolve the SQLite database path."""
    from .db import resolve_db_path  # type: ignore[import-untyped]

    return resolve_db_path()


def _get_yaml_source_dir() -> Path:
    """Return the directory containing example provider config YAMLs."""
    return Path("examples").resolve()


# ---------------------------------------------------------------------------
# PipelineRunner
# ---------------------------------------------------------------------------


class PipelineRunner:
    """Bridge between async web layer and synchronous HarvesterApp pipeline.

    Lifecycle:
        1. ``run_scan(provider)`` — validates, creates DB record, starts thread
        2. ``_execute(provider, run_id)`` — runs in thread, does the real scan
        3. ``_on_completed(provider, run_id)`` — fire-and-forget push hook
        4. ``list_runs()`` / ``get_run()`` / ``cancel_run()`` — query/control
    """

    # Allow tests to bypass __init__ and set fields directly:
    # test runner does ``PipelineRunner.__new__(PipelineRunner)``.
    _init_executor: bool = True
    _init_yaml_source_dir: str | None = None

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="harvester-pool"
        )
        self._running: dict[str, str] = {}  # provider → run_id
        self._locks: dict[str, threading.Lock] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._workspace = _load_workspace()
        self._db_path = _get_db_path()
        self._yaml_source_dir = _get_yaml_source_dir()
        # Proxy round-robin state (HARVESTER_PROXY comma-separated list)
        self._proxy_index = 0
        self._proxy_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API (async — called from FastAPI routes)
    # ------------------------------------------------------------------

    async def run_scan(
        self, provider_name: str, config_file: str | None = None
    ) -> str:
        """Start a scan for *provider_name* in a background thread.

        *config_file* optionally names the source config YAML (from
        ``schedule_config``); when None, the ``config-{provider_name}.yaml``
        convention is used.

        Returns the generated *run_id* (UUID4 string).

        Raises:
            ValueError: provider not configured / temp YAML cannot be generated.
            HTTPException(409): provider is already running.
        """
        # --- Validate provider has a config template ---
        source_yaml = self._resolve_source_yaml(provider_name, config_file)
        if not source_yaml.exists():
            raise ValueError(
                f"No example config for provider '{provider_name}': "
                f"expected {source_yaml}"
            )

        # --- Prevent concurrent scans for the same provider ---
        lock = self._provider_lock(provider_name)
        with lock:
            if provider_name in self._running:
                from fastapi import HTTPException  # type: ignore[import-untyped]

                raise HTTPException(
                    status_code=409,
                    detail=f"Provider '{provider_name}' is already running "
                    f"(run_id={self._running[provider_name]})",
                )

            run_id = str(uuid.uuid4())

            # Create initial DB record
            temp_yaml_path = self._temp_yaml_path(provider_name, run_id)
            await self._insert_run_record(
                run_id=run_id,
                provider_name=provider_name,
                config_file=str(temp_yaml_path),
                status="running",
            )

            self._running[provider_name] = run_id

        # --- Submit to background thread ---
        t = threading.Thread(
            target=self._execute,
            args=(provider_name, run_id, config_file),
            daemon=True,
            name=f"harvester-{provider_name}-{run_id[:8]}",
        )
        t.start()

        logger.info(
            f"Scan started: provider={provider_name} run_id={run_id}"
        )
        return run_id

    async def list_runs(
        self,
        provider: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query run_records with optional filters, paginated."""
        query = "SELECT * FROM run_records WHERE 1=1"
        params: list[Any] = []

        if provider:
            query += " AND provider_name = ?"
            params.append(provider)
        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        return await self._query_runs(query, params)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return a single run record by *run_id*, or None."""
        rows = await self._query_runs(
            "SELECT * FROM run_records WHERE id = ?", [run_id]
        )
        return rows[0] if rows else None

    async def cancel_run(self, run_id: str) -> bool:
        """Cancel a running scan by *run_id*.

        Returns True if the cancel event was set; False if the run was not
        in 'running' state.
        """
        run = await self.get_run(run_id)
        if run is None:
            return False
        if run["status"] != "running":
            return False

        # Set the cancel event if it exists
        cancel_event = self._cancel_events.get(run_id)
        if cancel_event is not None:
            cancel_event.set()

        provider_name = run["provider_name"]
        with self._provider_lock(provider_name):
            self._running.pop(provider_name, None)

        import aiosqlite

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """UPDATE run_records
                   SET status='cancelled',
                       finished_at=datetime('now')
                   WHERE id=?""",
                (run_id,),
            )
            await db.commit()

        logger.info(f"Run cancelled: run_id={run_id}")
        return True

    # ------------------------------------------------------------------
    # Background execution (runs in thread — synchronous)
    # ------------------------------------------------------------------

    def _execute(
        self,
        provider_name: str,
        run_id: str,
        config_file: str | None = None,
    ) -> None:
        """Run the full HarvesterApp scan in this thread.

        This method is **blocking** and must run in a background thread.
        """
        temp_yaml_path: Path | None = None
        cancel_event = threading.Event()
        self._cancel_events[run_id] = cancel_event

        try:
            # 1. Read enabled API tokens from DB
            tokens = self._get_enabled_api_tokens()
            if not tokens:
                raise RuntimeError(
                    f"No enabled API tokens found. "
                    f"Add tokens via the Token API before scanning."
                )

            # 2. Generate temporary YAML with injected tokens
            temp_yaml_path = self._generate_temp_yaml(
                provider_name, run_id, tokens, config_file=config_file
            )
            logger.info(
                f"Temp YAML created: {temp_yaml_path} "
                f"(run_id={run_id})"
            )

            # 3. Run HarvesterApp
            start_time = time.time()

            # HarvesterApp._setup_signal_handlers() calls signal.signal() which
            # is only legal in the main thread. We run scans in worker threads,
            # so neutralise signal registration here (shutdown is driven by the
            # pipeline completion event, not OS signals).
            import signal as _signal

            def _noop_signal(signum: int, frame: object) -> None:
                pass

            _orig_signal = _signal.signal
            if threading.current_thread() is not threading.main_thread():
                _signal.signal = _noop_signal  # type: ignore[assignment]

            from main import HarvesterApp  # type: ignore[import-untyped]

            app = HarvesterApp(str(temp_yaml_path))
            ok = app.initialize()
            if not ok:
                raise RuntimeError(
                    f"HarvesterApp.initialize() failed for {provider_name}"
                )

            # Register completion listener (T6 push hook)
            if app.task_manager is not None:
                app.task_manager.add_completion_listener(
                    lambda: self._on_completed(provider_name, run_id)
                )

            # Set shutdown_event on cancel
            if cancel_event.is_set():
                app.shutdown_event.set()
                raise RuntimeError("Run cancelled before start")

            # Block until pipeline completes
            app.run()

            # 4. Collect stats
            duration = round(time.time() - start_time, 2)
            valid_keys = self._count_valid_keys(app)

            # 5. Update DB — completed
            self._update_run_sync(
                run_id=run_id,
                status="completed",
                finished_at=True,
                duration_seconds=duration,
                valid_keys_found=valid_keys,
            )
            logger.info(
                f"Scan completed: provider={provider_name} "
                f"run_id={run_id} valid_keys={valid_keys} duration={duration}s"
            )

            # 6. Fire the push hook directly. The completion-listener path
            # (registered above) is unreliable from worker threads — the
            # CompletionEventManager may already be marked notified by the
            # status polling loop, or the callback thread never runs. Pushing
            # here, in the scan thread right after completion, guarantees the
            # valid keys reach gpt-load. gpt-load's add-multiple is idempotent,
            # so a duplicate push from the listener is harmless.
            #
            # A single config file can define multiple regional tasks (e.g.
            # config-mimo.yaml -> mimo-cn + mimo-sg), each writing its own
            # valid-keys.txt. Push every task, not just the schedule's
            # provider_name, so secondary regions reach gpt-load too.
            self._push_completed_tasks(provider_name, run_id, temp_yaml_path)

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error(
                f"Scan failed: provider={provider_name} run_id={run_id} "
                f"error={error_msg}"
            )
            self._update_run_sync(
                run_id=run_id,
                status="failed",
                finished_at=True,
                error_message=error_msg,
            )

        finally:
            # Restore signal handler patch (thread-local scope ended)
            if "signal" in dir() and "_orig_signal" in dir():
                try:
                    _signal.signal = _orig_signal  # type: ignore[assignment]
                except Exception:
                    pass

            # Clean up temp YAML
            if temp_yaml_path is not None and temp_yaml_path.exists():
                try:
                    temp_yaml_path.unlink()
                    logger.debug(
                        f"Temp YAML cleaned: {temp_yaml_path}"
                    )
                except OSError:
                    pass

            # Remove from running dict
            with self._provider_lock(provider_name):
                self._running.pop(provider_name, None)

            # Remove cancel event
            self._cancel_events.pop(run_id, None)

    # ------------------------------------------------------------------
    # Completion hook (T6 push integration)
    # ------------------------------------------------------------------

    def _on_completed(self, provider_name: str, run_id: str) -> None:
        """Fire-and-forget push notification (T6 integration point).

        Called from HarvesterApp's completion listener (in the scan thread).
        Tries to import ``web.push``; if not available (T6 not yet built),
        logs a message and continues.
        """
        try:
            from web.push import get_push_service  # type: ignore[import-untyped,unused-ignore]

            push_service = get_push_service()
            # Run push in a new thread to avoid blocking the completion callback
            t = threading.Thread(
                target=push_service.push_valid_keys,
                args=(provider_name, run_id),
                daemon=True,
            )
            t.start()
            logger.info(
                f"Push triggered: provider={provider_name} run_id={run_id}"
            )
        except ImportError:
            logger.info(
                f"Push service not available (T6 pending): "
                f"provider={provider_name} run_id={run_id}"
            )
        except Exception as exc:
            logger.error(
                f"Push hook error: provider={provider_name} "
                f"run_id={run_id} error={exc}"
            )

        # Tavily push — symmetric to the gpt-load block above. Only fires for
        # tavily scans; env gating lives inside TavilyPushService (it no-ops
        # when TAVILY_PROXY_BASE_URL / TAVILY_PROXY_AUTH_KEY are unset).
        try:
            from web.tavily_push import get_tavily_push_service  # type: ignore[import-untyped,unused-ignore]

            if provider_name == "tavily":
                tavily_push_service = get_tavily_push_service()
                # Run push in a new thread to avoid blocking the completion callback
                t = threading.Thread(
                    target=tavily_push_service.push_valid_keys,
                    args=(provider_name, run_id),
                    daemon=True,
                )
                t.start()
                logger.info(
                    f"Tavily push triggered: "
                    f"provider={provider_name} run_id={run_id}"
                )
        except ImportError:
            logger.info(
                f"Tavily push service not available: "
                f"provider={provider_name} run_id={run_id}"
            )
        except Exception as exc:
            logger.error(
                f"Tavily push hook error: provider={provider_name} "
                f"run_id={run_id} error={exc}"
            )

        # Self-bootstrap — symmetric to the tavily block above. Only fires for
        # github scans; env gating lives inside SelfBootstrapPushService (it
        # no-ops when HARVESTER_SELF_BOOTSTRAP=0).
        try:
            from web.self_bootstrap_push import get_self_bootstrap_push_service  # type: ignore[import-untyped,unused-ignore]

            if provider_name == "github":
                bootstrap_service = get_self_bootstrap_push_service()
                # Run push in a new thread to avoid blocking the completion callback
                t = threading.Thread(
                    target=bootstrap_service.push_valid_keys,
                    args=(provider_name, run_id),
                    daemon=True,
                )
                t.start()
                logger.info(
                    f"Self-bootstrap push triggered: "
                    f"provider={provider_name} run_id={run_id}"
                )
        except ImportError:
            logger.info(
                f"Self-bootstrap push service not available: "
                f"provider={provider_name} run_id={run_id}"
            )
        except Exception as exc:
            logger.error(
                f"Self-bootstrap push hook error: provider={provider_name} "
                f"run_id={run_id} error={exc}"
            )

    def _task_names_from_config(self, config_path: Path) -> list[str]:
        """Return the names of every task defined in a config YAML.

        A single config file can hold multiple regional tasks (e.g.
        ``config-mimo.yaml`` defines both ``mimo-cn`` and ``mimo-sg``), each
        writing its own ``valid-keys.txt``. Returns an empty list on any
        parse/read error or when no ``tasks`` list is present.
        """
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return []
        if not isinstance(raw, dict):
            return []
        tasks = raw.get("tasks")
        if not isinstance(tasks, list):
            return []
        return [
            str(t["name"])
            for t in tasks
            if isinstance(t, dict) and t.get("name")
        ]

    def _push_completed_tasks(
        self,
        provider_name: str,
        run_id: str,
        config_path: Path | None,
    ) -> None:
        """Fire the push hook for every task in the completed scan's config.

        The scan runs the whole config file (all regional tasks), so the push
        must fire once per task — not just for the schedule's ``provider_name``.
        Falls back to ``provider_name`` when the config is unavailable or
        defines no tasks.
        """
        task_names = self._task_names_from_config(config_path) if config_path else []
        for name in task_names or [provider_name]:
            self._on_completed(name, run_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _provider_lock(self, provider_name: str) -> threading.Lock:
        """Return (creating if needed) the lock for *provider_name*."""
        if provider_name not in self._locks:
            self._locks[provider_name] = threading.Lock()
        return self._locks[provider_name]

    def _resolve_source_yaml(
        self, provider_name: str, config_file: str | None = None
    ) -> Path:
        """Return the path to the source config YAML for *provider_name*.

        When *config_file* is given (e.g. from ``schedule_config``), it is
        resolved relative to the repo root (the parent of the examples dir);
        absolute paths are used as-is.  Otherwise the
        ``config-{provider_name}.yaml`` convention under the examples dir
        is used.
        """
        source_dir = (
            Path(self._init_yaml_source_dir)
            if self._init_yaml_source_dir
            else self._yaml_source_dir
        )
        if config_file:
            p = Path(config_file)
            if not p.is_absolute():
                return source_dir.parent / p
            return p
        return source_dir / f"config-{provider_name}.yaml"

    def _temp_yaml_path(self, provider_name: str, run_id: str) -> Path:
        """Return the path where the temporary YAML will be written."""
        workspace = Path(self._workspace)
        runtime_dir = workspace / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        return runtime_dir / f"config-{provider_name}-{run_id}.yaml"

    def _generate_temp_yaml(
        self,
        provider_name: str,
        run_id: str,
        tokens: list[str],
        config_file: str | None = None,
    ) -> Path:
        """Copy source YAML → temp YAML, injecting real GitHub tokens.

        - Reads ``examples/config-{provider_name}.yaml`` (or *config_file*
          when given)
        - Replaces ``global.github_credentials.tokens`` with *tokens*
        - Sets ``global.github_credentials.sessions`` to empty list
        - Writes to ``{workspace}/runtime/config-{provider_name}-{run_id}.yaml``

        Returns the path to the generated temp YAML.
        """
        source = self._resolve_source_yaml(provider_name, config_file)
        if not source.exists():
            raise ValueError(
                f"Source config not found: {source}"
            )

        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        if raw is None:
            raw = {}

        # Inject credentials
        raw.setdefault("global", {})
        global_section = raw["global"]
        if not isinstance(global_section, dict):
            raise ValueError(
                f"'global' section in {source} is not a mapping"
            )
        global_section.setdefault("github_credentials", {})
        creds = global_section["github_credentials"]
        if not isinstance(creds, dict):
            raise ValueError(
                f"'global.github_credentials' in {source} is not a mapping"
            )
        creds["tokens"] = list(tokens)
        creds["sessions"] = []

        # Optional outbound proxy for GitHub fetches (gather/check stages).
        # HARVESTER_PROXY supports a comma-separated list of proxies, e.g.
        # "socks5://host:1080,socks5://host:1090" — one is picked per scan in
        # round-robin fashion so concurrent scans spread across proxies.
        # pipeline.py calls client.set_proxy(global.proxy) at startup, so the
        # proxy applies to all GitHub HTTP traffic of this scan.
        # NOTE: source configs (config-*.yaml) often carry `proxy: ""` to
        # disable env-proxy inheritance — an explicit picked proxy must
        # override that key, otherwise the scan silently runs without it.
        proxy = self._pick_proxy()
        if proxy:
            global_section["proxy"] = proxy
        elif "proxy" not in global_section:
            # Absent key → loader inherits http(s)_proxy env; explicit "" would
            # disable it. Leave absent so container env proxies still work.
            pass

        # Force the workspace to the runner's own (HARVESTER_WORKSPACE). The
        # source configs carry per-provider workspaces like "./data-mimo" for
        # standalone CLI runs; without this override, the web scan would write
        # results to an ephemeral dir the push service never reads.
        global_section["workspace"] = str(self._workspace)

        # Write
        dest = self._temp_yaml_path(provider_name, run_id)
        dest.write_text(
            yaml.dump(raw, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )
        return dest

    def _pick_proxy(self) -> str:
        """Pick one proxy from HARVESTER_PROXY (comma-separated), round-robin.

        Returns "" when the env var is unset/empty.
        """
        raw = os.environ.get("HARVESTER_PROXY", "").strip()
        if not raw:
            return ""
        proxies = [p.strip() for p in raw.split(",") if p.strip()]
        if not proxies:
            return ""
        if len(proxies) == 1:
            return proxies[0]
        # Thread-safe round-robin; fall back to plain counter when the
        # instance was created via __new__ (unit tests).
        lock = getattr(self, "_proxy_lock", None) or threading.Lock()
        with lock:
            idx = getattr(self, "_proxy_index", 0)
            proxy = proxies[idx % len(proxies)]
            self._proxy_index = idx + 1
        return proxy

    def _get_enabled_api_tokens(self) -> list[str]:
        """Read enabled API tokens from the github_tokens table (synchronous).

        Returns a list of decrypted plaintext token strings.
        Only returns tokens with ``token_type='api' AND enabled=1``.
        """
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """SELECT token_encrypted FROM github_tokens
                   WHERE token_type='api' AND enabled=1"""
            )
            rows = cursor.fetchall()
        finally:
            conn.close()

        tokens: list[str] = []
        for row in rows:
            try:
                plain = decrypt_str(row["token_encrypted"])
                tokens.append(plain)
            except ValueError as exc:
                logger.warning(
                    f"Failed to decrypt token: {exc}"
                )
        return tokens

    def _count_valid_keys(self, app: Any) -> int:
        """Extract valid key count from a completed HarvesterApp instance.

        Tries ``task_manager.stats().resource.valid`` first;
        falls back to reading ``valid-keys.txt``.
        """
        try:
            if app.task_manager is not None:
                stats = app.task_manager.stats()
                if hasattr(stats, "resource") and hasattr(
                    stats.resource, "valid"
                ):
                    return int(stats.resource.valid)
        except Exception:
            pass

        # Fallback: read valid-keys.txt from workspace
        try:
            workspace = app.config.global_config.workspace if app.config else "./data"
            provider_dir = (
                Path(workspace) / "providers"
            )
            if provider_dir.exists():
                for child in provider_dir.iterdir():
                    vk = child / "valid-keys.txt"
                    if vk.exists():
                        text = vk.read_text(encoding="utf-8")
                        return sum(
                            1 for line in text.splitlines() if line.strip()
                        )
        except Exception:
            pass

        return 0

    def _update_run_sync(
        self,
        run_id: str,
        status: str,
        finished_at: bool = False,
        duration_seconds: float | None = None,
        valid_keys_found: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update a run record from the scan thread (synchronous sqlite3)."""
        conn = sqlite3.connect(self._db_path)
        try:
            parts = ["status = ?"]
            params: list[Any] = [status]

            if finished_at:
                parts.append("finished_at = datetime('now')")
            if duration_seconds is not None:
                parts.append("duration_seconds = ?")
                params.append(duration_seconds)
            if valid_keys_found is not None:
                parts.append("valid_keys_found = ?")
                params.append(valid_keys_found)
            if error_message is not None:
                parts.append("error_message = ?")
                params.append(error_message)

            params.append(run_id)
            conn.execute(
                f"UPDATE run_records SET {', '.join(parts)} WHERE id = ?",
                params,
            )
            conn.commit()
        finally:
            conn.close()

    async def _insert_run_record(
        self,
        run_id: str,
        provider_name: str,
        config_file: str,
        status: str,
    ) -> None:
        """Insert a new run_records row (async)."""
        import aiosqlite

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT INTO run_records
                   (id, provider_name, config_file, status)
                   VALUES (?, ?, ?, ?)""",
                (run_id, provider_name, config_file, status),
            )
            await db.commit()

    async def _query_runs(
        self, query: str, params: list[Any]
    ) -> list[dict[str, Any]]:
        """Execute a SELECT query on run_records and return list of dicts."""
        import aiosqlite

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_runner: PipelineRunner | None = None


def get_runner() -> PipelineRunner:
    """Return the module-level PipelineRunner singleton, creating it if needed."""
    global _runner
    if _runner is None:
        _runner = PipelineRunner()
    return _runner
