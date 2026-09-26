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
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from tools.logger import get_logger
from tools.patterns import redact_api_keys_in_text

from .crypto import _get_crypto, decrypt_str
from .models import mask_token

logger = get_logger("web.runner")

# Zero-yield tripwire threshold (audit JOB 1c): a corpus larger than this
# that produced ZERO candidate materials is the historical "healthy-looking
# scan, zero yield" signature — four rounds of debugging (glm's wrong key
# pattern, groq's decoy pool, ollama's no-op created: qualifier, modelscope's
# naked pattern) all looked like completed runs with valid_keys_found=0 and
# nothing in the row said the EXTRACTION stage was the silent failure point.
_ZERO_YIELD_LINK_THRESHOLD = 1000

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

            # Register the cancel event BEFORE the row insert and thread
            # start. cancel_run looks events up by run_id, and the scan
            # thread honours a pre-set event before starting the pipeline;
            # creating it only inside _execute used to drop cancels that
            # arrived between the row insert and the thread body.
            self._cancel_events[run_id] = threading.Event()

            try:
                # Create initial DB record
                temp_yaml_path = self._temp_yaml_path(provider_name, run_id)
                await self._insert_run_record(
                    run_id=run_id,
                    provider_name=provider_name,
                    config_file=str(temp_yaml_path),
                    status="running",
                )

                self._running[provider_name] = run_id
            except BaseException:
                # No thread will run for this run_id — don't leak the event.
                self._cancel_events.pop(run_id, None)
                raise

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
        """Request cancellation of a running scan by *run_id*.

        Cancellation is COOPERATIVE and best-effort: the scan thread checks
        the cancel event only BEFORE starting the pipeline, so a mid-run
        cancel CANNOT stop ``HarvesterApp.run()`` — the thread keeps
        executing until the pipeline exits on its own.

        Therefore this method deliberately does NOT release the provider
        slot in ``self._running``: the scan thread's ``finally`` block is
        the single owner of the guard release. Freeing the slot here would
        let a second scan for the same provider start while the first is
        still alive, with both writing the same ``providers/{task}/``
        result files.

        The status write is conditional on the row still being 'running'
        so it cannot race a terminal write from the scan thread.

        Returns True if the row was flipped to 'cancelled'; False if the
        run was not (or no longer was) in 'running' state.
        """
        run = await self.get_run(run_id)
        if run is None:
            return False
        if run["status"] != "running":
            return False

        # Set the cancel event if it exists (run_scan registers it before
        # the row insert, so an early cancel is never dropped).
        cancel_event = self._cancel_events.get(run_id)
        if cancel_event is not None:
            cancel_event.set()

        import aiosqlite

        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """UPDATE run_records
                   SET status='cancelled',
                       finished_at=datetime('now')
                   WHERE id=? AND status='running'""",
                (run_id,),
            )
            await db.commit()

        if cursor.rowcount == 0:
            # The row left 'running' between the read and the write (the
            # scan thread finished/failed first) — nothing was cancelled.
            return False

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
        # run_scan pre-registers the event before the row insert/thread
        # start so an early cancel is never dropped; setdefault keeps
        # direct _execute calls (tests) working.
        cancel_event = self._cancel_events.setdefault(
            run_id, threading.Event()
        )

        try:
            # Honour a cancel that landed between run_scan registering this
            # event and the thread body starting — the row already reads
            # 'cancelled', and the conditional failed-write below is a no-op.
            if cancel_event.is_set():
                raise RuntimeError("Run cancelled before start")

            # 1. Read enabled API tokens from DB
            tokens = self._get_enabled_api_tokens()
            if not tokens:
                raise RuntimeError(
                    "No enabled API tokens found. "
                    "Add tokens via the Token API before scanning."
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

            # Snapshot already-known valid keys for each task in this config
            # BEFORE running, so the per-run delta (post − pre) identifies
            # only the keys newly added by THIS run.
            task_names = self._task_names_from_config(temp_yaml_path) or [
                provider_name
            ]
            before_keys = self._snapshot_valid_keys(task_names)

            # Block until pipeline completes
            app.run()

            # 4. Collect stats
            duration = round(time.time() - start_time, 2)
            valid_keys = self._count_valid_keys(app, task_names)
            links_total, materials_total = self._count_pipeline_totals(app)

            # Zero-yield tripwire (observability guard): a large corpus with
            # ZERO extracted candidates is the historical "healthy-looking
            # scan, zero yield" failure class (glm's wrong key pattern,
            # groq's decoy pool, ollama's no-op qualifier, modelscope's
            # naked pattern — four debugging rounds before anything in the
            # record said extraction was the silent failure point).
            #
            # WHY error_message and NOT a new status value: run_records.status
            # is DB CHECK-constrained to ('running','completed','failed',
            # 'cancelled'), and the scheduler, the runs UI filters and the
            # startup reconciliation branch on exactly those four — a fifth
            # terminal value ('degraded') would break all of them. The scan
            # genuinely COMPLETED; degraded yield is a quality flag layered
            # on top, and error_message is rendered by the run-detail UI and
            # returned by the runs API for every row regardless of status.
            degradation = self._zero_yield_degradation(
                provider_name, run_id, links_total, materials_total, app
            )
            if degradation is None:
                degradation = self._no_work_degradation(
                    provider_name, run_id, links_total, materials_total, valid_keys
                )

            # 5. Update DB — completed. Conditional on the row still being
            # 'running': a cancel_run that landed mid-scan has already
            # written 'cancelled', and this write must not flip it back.
            self._update_run_sync(
                run_id=run_id,
                status="completed",
                finished_at=True,
                duration_seconds=duration,
                valid_keys_found=valid_keys,
                links_total=links_total,
                materials_total=materials_total,
                error_message=degradation,
                only_if_running=True,
            )
            logger.info(
                f"Scan completed: provider={provider_name} "
                f"run_id={run_id} valid_keys={valid_keys} "
                f"links={links_total} materials={materials_total} "
                f"duration={duration}s"
            )

            # 6. Record newly-added valid keys (delta since scan start) for
            # the dashboard. Best-effort — a capture failure must never fail
            # or affect the run outcome.
            try:
                after_keys = self._snapshot_valid_keys(task_names)
                new_count = self._record_new_keys(
                    run_id, provider_name, before_keys, after_keys
                )
                if new_count:
                    logger.info(
                        f"Captured {new_count} new valid key(s): "
                        f"provider={provider_name} run_id={run_id}"
                    )
            except Exception as exc:
                logger.warning(
                    f"New-key capture failed: run_id={run_id} error={exc}"
                )

            # 7. Fire the push hook directly. The completion-listener path
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
            # The same text is surfaced in the runs UI and persisted to
            # run_records.error_message. Exception strings can embed tokens
            # (e.g. a URL with an api_key query string from a failed request),
            # and the DB write bypasses the logger's RedactionFilter — so
            # redact before the message leaves this branch.
            error_msg = redact_api_keys_in_text(f"{type(exc).__name__}: {exc}")
            logger.error(
                f"Scan failed: provider={provider_name} run_id={run_id} "
                f"error={error_msg}"
            )
            # Failed runs still get their wall-clock duration (started_at →
            # now, computed in SQL — the Python-side start_time may never
            # have been set) and a valid-key count scoped to THIS run's own
            # tasks. Conditional on 'running' so a 'cancelled' row written
            # by cancel_run is never flipped to 'failed'.
            failed_valid_keys = self._count_valid_keys_for_failed_run(
                provider_name, temp_yaml_path
            )
            self._update_run_sync(
                run_id=run_id,
                status="failed",
                finished_at=True,
                valid_keys_found=failed_valid_keys,
                error_message=error_msg,
                duration_from_started_at=True,
                only_if_running=True,
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

            # Release the provider guard — only if the slot still belongs
            # to THIS run. cancel_run deliberately does NOT free the guard
            # (a mid-run cancel cannot stop this thread; see cancel_run),
            # and popping a foreign run_id's entry here would hand out a
            # slot that a different scan owns (cascading guard loss).
            with self._provider_lock(provider_name):
                if self._running.get(provider_name) == run_id:
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

        # Serpapi push — symmetric to the tavily block above. Only fires for
        # serpapi scans; env gating lives inside SerpapiPushService (it no-ops
        # when SERPAPI_PROXY_BASE_URL / SERPAPI_PROXY_AUTH_KEY are unset).
        try:
            from web.serpapi_push import get_serpapi_push_service  # type: ignore[import-untyped,unused-ignore]

            if provider_name == "serpapi":
                serpapi_push_service = get_serpapi_push_service()
                # Run push in a new thread to avoid blocking the completion callback
                t = threading.Thread(
                    target=serpapi_push_service.push_valid_keys,
                    args=(provider_name, run_id),
                    daemon=True,
                )
                t.start()
                logger.info(
                    f"Serpapi push triggered: provider={provider_name} run_id={run_id}"
                )
        except ImportError:
            logger.info(
                f"Serpapi push service not available: provider={provider_name} run_id={run_id}"
            )
        except Exception as exc:
            logger.error(
                f"Serpapi push hook error: provider={provider_name} "
                f"run_id={run_id} error={exc}"
            )

        # Agnes AI push — symmetric to the serpapi block above. Only fires for
        # agnes-ai scans; the target gpt-load instance + group default via
        # AGNES_LOAD_BASE_URL / AGNES_LOAD_GROUP_ID (+ optional
        # AGNES_LOAD_AUTH_KEY). Env gating lives inside AgnesAIPushService.
        try:
            from web.agnes_ai_push import get_agnes_ai_push_service  # type: ignore[import-untyped,unused-ignore]

            if provider_name == "agnes-ai":
                agnes_ai_push_service = get_agnes_ai_push_service()
                # Run push in a new thread to avoid blocking the completion callback
                t = threading.Thread(
                    target=agnes_ai_push_service.push_valid_keys,
                    args=(provider_name, run_id),
                    daemon=True,
                )
                t.start()
                logger.info(
                    f"AgnesAI push triggered: provider={provider_name} run_id={run_id}"
                )
        except ImportError:
            logger.info(
                f"AgnesAI push service not available: "
                f"provider={provider_name} run_id={run_id}"
            )
        except Exception as exc:
            logger.error(
                f"AgnesAI push hook error: provider={provider_name} "
                f"run_id={run_id} error={exc}"
            )

        # ModelScope push — symmetric to the agnes-ai block above. Only fires
        # for modelscope scans; the target gpt-load instance + group default
        # via MODELSCOPE_LOAD_BASE_URL / MODELSCOPE_LOAD_GROUP_ID (+ optional
        # MODELSCOPE_LOAD_AUTH_KEY). Env gating lives inside
        # ModelScopePushService.
        try:
            from web.modelscope_push import get_modelscope_push_service  # type: ignore[import-untyped,unused-ignore]

            if provider_name == "modelscope":
                modelscope_push_service = get_modelscope_push_service()
                # Run push in a new thread to avoid blocking the completion callback
                t = threading.Thread(
                    target=modelscope_push_service.push_valid_keys,
                    args=(provider_name, run_id),
                    daemon=True,
                )
                t.start()
                logger.info(
                    f"ModelScope push triggered: "
                    f"provider={provider_name} run_id={run_id}"
                )
        except ImportError:
            logger.info(
                f"ModelScope push service not available: "
                f"provider={provider_name} run_id={run_id}"
            )
        except Exception as exc:
            logger.error(
                f"ModelScope push hook error: provider={provider_name} "
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
        proxy = self._pick_proxy(provider_name)
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

    def _pick_proxy(self, provider_name: str = "") -> str:
        """Pick one proxy for a scan, from a comma-separated rotation.

        Per-provider override: ``HARVESTER_PROXY_<PROVIDER>`` (provider name
        upper-cased, non-alphanumerics mapped to ``_``, e.g.
        ``HARVESTER_PROXY_GROQ``) replaces the global ``HARVESTER_PROXY``
        rotation for that provider when set and non-empty. Providers without
        an override keep the global rotation (one proxy per scan, spread
        across concurrent scans).

        Returns "" when nothing is configured.
        """
        raw = ""
        if provider_name:
            override_var = "HARVESTER_PROXY_" + re.sub(
                r"[^A-Za-z0-9]+", "_", provider_name
            ).upper()
            raw = os.environ.get(override_var, "").strip()
        if not raw:
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

    def _count_valid_keys(self, app: Any, task_names: list[str]) -> int:
        """Extract valid key count from a completed HarvesterApp instance.

        Tries ``task_manager.stats().resource.valid`` first; falls back to
        reading ``valid-keys.txt`` — but ONLY for the given *task_names* of
        this run. The fallback never iterates foreign provider directories:
        reading the first ``valid-keys.txt`` found under ``providers/`` made
        a run report another provider's count.
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

        # Fallback: read this run's own task dirs only. A multi-task config
        # (e.g. mimo-cn + mimo-sg) sums across its tasks; unrelated provider
        # dirs and backup-* folders are never touched.
        try:
            workspace = app.config.global_config.workspace if app.config else "./data"
            snapshot = self._snapshot_valid_keys(task_names, Path(workspace))
            return sum(len(keys) for keys in snapshot.values())
        except Exception:
            pass

        return 0

    def _count_pipeline_totals(self, app: Any) -> tuple[int | None, int | None]:
        """Extract ``(links_total, materials_total)`` from a completed app.

        Primary path mirrors :meth:`_count_valid_keys` — the aggregated
        ``task_manager.stats().resource`` counters (LINKS/MATERIAL results
        the pipeline persisted for this run's tasks).  Returns
        ``(None, None)`` when stats are unavailable (mocked app, dead
        task_manager, malformed counters); the DB write then skips the
        columns entirely, so pre-migration databases and mocked-app tests
        behave exactly as before this feature existed.
        """
        try:
            if app.task_manager is None:
                return (None, None)
            resource = app.task_manager.stats().resource
            return (int(resource.links), int(resource.material))
        except Exception:
            return (None, None)

    def _gather_counter_snapshot(self, app: Any) -> dict[str, int]:
        """Sum the per-provider gather-outcome counters of this run.

        Reads the live ResultManagers through
        ``app.task_manager.pipeline.result_manager.managers`` (see
        storage.persistence: GATHER_OK / GATHER_EMPTY / GATHER_ERROR_*).
        Returns ``{}`` on any failure — the counters only enrich the
        zero-yield message and must never break the completion path.
        """
        try:
            managers = app.task_manager.pipeline.result_manager.managers
            totals: dict[str, int] = {}
            for manager in managers.values():
                for name, value in manager.gather_counters().items():
                    totals[name] = totals.get(name, 0) + int(value)
            return totals
        except Exception:
            return {}

    def _zero_yield_degradation(
        self,
        provider_name: str,
        run_id: str,
        links_total: int | None,
        materials_total: int | None,
        app: Any,
    ) -> str | None:
        """Return the zero-yield degradation marker, or None when healthy.

        Fires when ``links_total > _ZERO_YIELD_LINK_THRESHOLD`` and
        ``materials_total == 0``: the corpus is large but extraction
        produced no candidates at all.  Logs ONE error line and returns the
        same message for persistence into ``run_records.error_message``
        (status stays 'completed' — see the rationale at the call site).
        The gather counters (when available) sharpen the suspicion: fetches
        succeeded (gather_ok ≈ corpus) points at the key_pattern; fetches
        failed en masse points at the gather transport.
        """
        if links_total is None or materials_total is None:
            return None
        if links_total <= _ZERO_YIELD_LINK_THRESHOLD or materials_total != 0:
            return None

        counters = self._gather_counter_snapshot(app)
        if counters:
            detail = (
                f"gather_ok={counters.get('gather_ok', 0)} "
                f"gather_empty={counters.get('gather_empty', 0)} "
                f"gather_error_404={counters.get('gather_error_404', 0)} "
                f"gather_error_other={counters.get('gather_error_other', 0)}"
            )
        else:
            detail = "gather counters unavailable"
        message = (
            f"zero-yield degradation: provider={provider_name} "
            f"run_id={run_id} links_total={links_total} "
            f"materials_total=0 — extraction produced NO candidates from a "
            f"large corpus; suspect key_pattern (wrong key format, decoy "
            f"pool, dead search qualifier) or gather transport failure "
            f"({detail})"
        )
        logger.error(message)
        return message

    def _no_work_degradation(
        self,
        provider_name: str,
        run_id: str,
        links_total: int | None,
        materials_total: int | None,
        valid_keys: int,
    ) -> str | None:
        """Marker for a 'completed' run that did NO work at all, or None.

        Measured 2026-09-26 08:00 (openrouter): the run had ONE condition, its
        single search task was denied by the process-wide ``github_api`` rate
        limiter on all three attempts, the retry budget dropped it silently and
        the pipeline finished in 54 s with links=0 / materials=0 / valid=0 —
        status 'completed' and ``error_message`` NULL. The zero-yield tripwire
        requires > ``_ZERO_YIELD_LINK_THRESHOLD`` links, so this class was
        invisible in run_records. Status stays 'completed' (the same four-value
        CHECK-constraint rationale as the zero-yield marker); the reason is
        recorded for the UI/API.
        """
        if links_total != 0 or materials_total != 0 or valid_keys:
            return None
        message = (
            f"no-work run: provider={provider_name} run_id={run_id} "
            f"links_total=0 materials_total=0 valid_keys=0 — the search stage "
            f"produced nothing (credential cooldown, rate-limiter denial, or "
            f"genuinely empty dorks); look for 'task dropped' lines in the "
            f"stage log"
        )
        logger.error(message)
        return message

    def _count_valid_keys_for_failed_run(
        self, provider_name: str, temp_yaml_path: Path | None
    ) -> int:
        """Best-effort valid-key count for a FAILED run, scoped to its tasks.

        Uses the task names from the run's generated config (falling back to
        *provider_name*), so a failed run reports at worst a stale count for
        its own provider directory and never another provider's numbers.
        Reads via :meth:`_snapshot_valid_keys`, which is non-raising.
        """
        task_names = (
            self._task_names_from_config(temp_yaml_path)
            if temp_yaml_path is not None
            else []
        ) or [provider_name]
        snapshot = self._snapshot_valid_keys(task_names)
        return sum(len(keys) for keys in snapshot.values())

    def _snapshot_valid_keys(
        self, task_names: list[str], workspace: Path | None = None
    ) -> dict[str, set[str]]:
        """Read each task's ``valid-keys.txt`` into a set of non-empty lines.

        Path: ``{workspace}/providers/{task_name}/valid-keys.txt``; the
        workspace defaults to the runner's own. Missing directory/file →
        empty set. Only the named tasks are read, so ``backup-*`` and stray
        folders under providers are never touched.
        """
        providers_dir = Path(workspace or self._workspace) / "providers"
        snapshots: dict[str, set[str]] = {}
        for name in task_names:
            keys_path = providers_dir / name / "valid-keys.txt"
            keys: set[str] = set()
            try:
                if keys_path.exists():
                    for line in keys_path.read_text(
                        encoding="utf-8"
                    ).splitlines():
                        stripped = line.strip().lstrip("\ufeff")
                        if stripped:
                            keys.add(stripped)
            except (OSError, UnicodeDecodeError):
                pass
            snapshots[name] = keys
        return snapshots

    def _diff_new_keys(
        self,
        before: dict[str, set[str]],
        after: dict[str, set[str]],
    ) -> dict[str, list[str]]:
        """Return per-task newly-added keys (``after - before``), sorted."""
        diff: dict[str, list[str]] = {}
        for task, after_keys in after.items():
            new_keys = sorted(after_keys - before.get(task, set()))
            if new_keys:
                diff[task] = new_keys
        return diff

    def _record_new_keys(
        self,
        run_id: str,
        provider_name: str,
        before: dict[str, set[str]],
        after: dict[str, set[str]],
    ) -> int:
        """Persist newly-added valid keys (masked + hashed only).

        Never stores plaintext. Dedups via ``UNIQUE(run_id, key_hash)``.
        Never raises — returns the inserted count (0 on any error).
        """
        diff = self._diff_new_keys(before, after)
        if not diff:
            return 0
        inserted = 0
        try:
            conn = sqlite3.connect(self._db_path)
            try:
                for task, keys in diff.items():
                    for key in keys:
                        try:
                            conn.execute(
                                "INSERT INTO run_new_keys "
                                "(run_id, provider_name, task_name, "
                                " key_hash, token_masked) VALUES (?, ?, ?, ?, ?)",
                                (
                                    run_id,
                                    provider_name,
                                    task,
                                    _get_crypto().hash_token(key),
                                    mask_token(key),
                                ),
                            )
                            inserted += 1
                        except sqlite3.IntegrityError:
                            logger.info(
                                f"run_new_keys: duplicate skipped "
                                f"(run_id={run_id} task={task})"
                            )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(f"run_new_keys capture failed: {exc}")
            return 0
        return inserted

    def _update_run_sync(
        self,
        run_id: str,
        status: str,
        finished_at: bool = False,
        duration_seconds: float | None = None,
        valid_keys_found: int | None = None,
        error_message: str | None = None,
        duration_from_started_at: bool = False,
        only_if_running: bool = False,
        links_total: int | None = None,
        materials_total: int | None = None,
    ) -> None:
        """Update a run record from the scan thread (synchronous sqlite3).

        ``duration_from_started_at`` computes the wall-clock duration in SQL
        (``started_at`` → now) — used by the failure path, where the
        Python-side start time may never have been recorded and the row's
        own ``started_at`` is the authoritative start.

        ``only_if_running`` restricts the UPDATE to rows still in 'running'
        state so a terminal write from the scan thread can never overwrite
        a 'cancelled' (or otherwise terminal) row.

        ``links_total`` / ``materials_total`` persist the run's corpus and
        candidate-extraction counters (zero-yield tripwire inputs). ``None``
        skips the column entirely, keeping the UPDATE compatible with
        pre-migration databases whose rows lack the columns.
        """
        conn = sqlite3.connect(self._db_path)
        try:
            parts = ["status = ?"]
            params: list[Any] = [status]

            if finished_at:
                parts.append("finished_at = datetime('now')")
            if duration_seconds is not None:
                parts.append("duration_seconds = ?")
                params.append(duration_seconds)
            if duration_from_started_at:
                parts.append(
                    "duration_seconds = CAST((julianday('now') "
                    "- julianday(started_at)) * 86400 AS REAL)"
                )
            if valid_keys_found is not None:
                parts.append("valid_keys_found = ?")
                params.append(valid_keys_found)
            if links_total is not None:
                parts.append("links_total = ?")
                params.append(links_total)
            if materials_total is not None:
                parts.append("materials_total = ?")
                params.append(materials_total)
            if error_message is not None:
                parts.append("error_message = ?")
                # Redact at the DB boundary too: error_message strings can
                # embed tokens and run_records is surfaced in the runs UI.
                params.append(redact_api_keys_in_text(error_message))

            where = "WHERE id = ?"
            if only_if_running:
                where += " AND status = 'running'"
            params.append(run_id)
            conn.execute(
                f"UPDATE run_records SET {', '.join(parts)} {where}",
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
