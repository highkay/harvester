#!/usr/bin/env python3

"""Jinja2 管理界面路由 — 纯服务端渲染页面（Wave 3 T7）。

提供仪表盘、Token 管理、调度管理、运行历史、运行详情、推送日志与登录
页面。除登录外所有页面均要求 Bearer 认证（``Depends(require_ui_session)``）。
数据由服务端直查 SQLite（``web.db``），模板不依赖任何前端框架；
Token 等敏感字段一律脱敏展示，绝不回显加密原文。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from tools.logger import get_logger

from .db import get_db, resolve_db_path
from .deps import get_current_user, get_settings
from .middleware import SESSION_COOKIE_NAME, authenticate_session

logger = get_logger("web.router_ui")

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _is_authenticated(request: Request) -> bool:
    """Return True when the request carries a valid session (or Bearer) auth."""
    try:
        get_current_user(request)
        return True
    except HTTPException:
        return False


# Expose login state to every template (nav shows 登录 vs 退出 accordingly).
templates.env.globals["is_authenticated"] = _is_authenticated


def require_ui_session(request: Request) -> None:
    """UI-page auth: redirect to /login when the session cookie is missing.

    Unlike ``get_current_user`` (which raises 401 JSON for API clients),
    browser page navigation should land on the login page instead of
    showing a raw JSON error.  Bearer-header clients are also accepted,
    so API tooling can still fetch rendered pages.
    """
    # Bearer header present → delegate to the normal auth dependency
    if request.headers.get("Authorization"):
        get_current_user(request)
        return

    settings = get_settings()
    try:
        authenticate_session(request, settings.web_auth_key)
    except HTTPException:
        raise HTTPException(
            status_code=303,
            detail="Please log in first",
            headers={"Location": "/login"},
        )


def _mask_token(token_hash: str) -> str:
    """脱敏展示：固定占位符 + 哈希前缀，绝不回显加密原文。"""
    return "****" + (token_hash[:8] if token_hash else "")


def _scan_supported_providers() -> list[str]:
    """Scan ``examples/config-*.yaml`` for every declared task name.

    Returns a de-duplicated, sorted list of all supported provider task
    types (deepseek, kimi, kimi-coding, mimo-cn, qwen-cn, ...) so the
    push-config page can offer every task — not just the seeded ones.
    """
    import re

    import yaml

    examples_dir = Path(__file__).resolve().parent.parent / "examples"
    providers: set[str] = set()
    pattern = re.compile(r"^\s*-\s*name:\s*(\S+)")
    if not examples_dir.is_dir():
        return []
    for cfg in sorted(examples_dir.glob("config-*.yaml")):
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        for task in data.get("tasks") or []:
            name = (task or {}).get("name")
            if name:
                providers.add(str(name).strip())
    return sorted(providers)


def _scan_config_mapping() -> dict[str, list[str]]:
    """Map every task name to the example config file(s) that declare it.

    Returns ``{task_name: [config_file, ...]}`` — e.g.
    ``{"kimi": ["examples/config-kimi.yaml"], "openai": ["examples/config-full.yaml", "examples/config-simple.yaml"]}``.
    A task may appear in several configs (e.g. openai in full + simple), so
    the value is a list; sorted for stable ordering.  Paths carry the
    ``examples/`` prefix so they match what schedule_config stores.
    """
    import yaml

    examples_dir = Path(__file__).resolve().parent.parent / "examples"
    mapping: dict[str, set[str]] = {}
    if not examples_dir.is_dir():
        return {}
    for cfg in sorted(examples_dir.glob("config-*.yaml")):
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        for task in data.get("tasks") or []:
            name = (task or {}).get("name")
            if name:
                mapping.setdefault(str(name).strip(), set()).add(f"examples/{cfg.name}")
    return {name: sorted(files) for name, files in sorted(mapping.items())}


# ---------------------------------------------------------------------------
# GET / — 仪表盘
# ---------------------------------------------------------------------------


@router.get("/")
async def dashboard(request: Request, _user: bool = Depends(require_ui_session)):
    """仪表盘：统计卡 + 调度表 + 最近运行/推送记录。"""
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        cur = await db.execute("SELECT COUNT(*) FROM github_tokens")
        token_total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM github_tokens WHERE enabled = 1")
        token_enabled = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM run_records")
        run_total = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COALESCE(SUM(valid_keys_found), 0) FROM run_records"
        )
        valid_keys = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT provider_name, cron_expression, enabled, config_file "
            "FROM schedule_config ORDER BY provider_name"
        )
        schedules = [dict(row) for row in await cur.fetchall()]

        cur = await db.execute(
            "SELECT id, provider_name, status, started_at, valid_keys_found "
            "FROM run_records ORDER BY started_at DESC LIMIT 5"
        )
        recent_runs = [dict(row) for row in await cur.fetchall()]

        cur = await db.execute(
            "SELECT provider_name, group_id, status, pushed_at "
            "FROM push_logs ORDER BY pushed_at DESC LIMIT 5"
        )
        recent_pushes = [dict(row) for row in await cur.fetchall()]
    finally:
        await db.close()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "page": "dashboard",
            "stats": {
                "token_total": token_total,
                "token_enabled": token_enabled,
                "run_total": run_total,
                "valid_keys": valid_keys,
            },
            "schedules": schedules,
            "recent_runs": recent_runs,
            "recent_pushes": recent_pushes,
        },
    )


# ---------------------------------------------------------------------------
# GET /tokens — Token 管理（脱敏展示）
# ---------------------------------------------------------------------------


@router.get("/tokens")
async def tokens_page(request: Request, _user: bool = Depends(require_ui_session)):
    """Token 全列表：仅展示 id / 类型 / 脱敏值 / 标签 / 启用状态。"""
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        # 不 SELECT token_encrypted —— 加密原文永不离开数据库
        cur = await db.execute(
            "SELECT id, token_type, token_hash, label, enabled, created_at "
            "FROM github_tokens ORDER BY id"
        )
        rows = [dict(row) for row in await cur.fetchall()]
    finally:
        await db.close()

    for t in rows:
        t["token_masked"] = _mask_token(t.pop("token_hash", None) or "")

    return templates.TemplateResponse(
        request=request,
        name="tokens.html",
        context={"page": "tokens", "tokens": rows},
    )


# ---------------------------------------------------------------------------
# GET /schedule — 调度管理
# ---------------------------------------------------------------------------


@router.get("/schedule")
async def schedule_page(request: Request, _user: bool = Depends(require_ui_session)):
    """调度配置全列表（含编辑/删除操作）。"""
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        cur = await db.execute(
            "SELECT id, provider_name, cron_expression, enabled, config_file, "
            "updated_at FROM schedule_config ORDER BY provider_name"
        )
        rows = [dict(row) for row in await cur.fetchall()]
    finally:
        await db.close()

    # Compute next_run_time for each schedule via the live scheduler
    from .scheduler import get_scheduler_service

    svc = get_scheduler_service()
    next_times: dict[str, str | None] = {}
    if svc is not None:
        for s in rows:
            try:
                job = svc._scheduler.get_job(f"scan-{s['provider_name']}")
                next_times[s["provider_name"]] = (
                    str(job.next_run_time) if job and job.next_run_time else None
                )
            except Exception:
                next_times[s["provider_name"]] = None
    for s in rows:
        s["next_run_time"] = next_times.get(s["provider_name"])

    return templates.TemplateResponse(
        request=request,
        name="schedule.html",
        context={
            "page": "schedule",
            "schedules": rows,
            "all_providers": _scan_supported_providers(),
            "config_mapping": _scan_config_mapping(),
        },
    )


# ---------------------------------------------------------------------------
# GET /runs — 运行历史
# ---------------------------------------------------------------------------


@router.get("/runs")
async def runs_page(request: Request, _user: bool = Depends(require_ui_session)):
    """运行记录全列表（run_id 截断展示）。"""
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        cur = await db.execute(
            "SELECT id, provider_name, status, started_at, finished_at, "
            "duration_seconds, valid_keys_found, total_keys_checked "
            "FROM run_records ORDER BY started_at DESC"
        )
        rows = [dict(row) for row in await cur.fetchall()]
    finally:
        await db.close()

    for r in rows:
        r["id_short"] = r["id"][:8]

    return templates.TemplateResponse(
        request=request,
        name="runs.html",
        context={"page": "runs", "runs": rows},
    )


# ---------------------------------------------------------------------------
# GET /runs/{run_id} — 运行详情
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}")
async def run_detail_page(
    run_id: str, request: Request, _user: bool = Depends(require_ui_session)
):
    """单条运行记录详情。"""
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        cur = await db.execute(
            "SELECT id, provider_name, config_file, status, started_at, "
            "finished_at, duration_seconds, valid_keys_found, "
            "total_keys_checked, error_message, created_at "
            "FROM run_records WHERE id = ?",
            (run_id,),
        )
        row = await cur.fetchone()
    finally:
        await db.close()

    if row is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")

    return templates.TemplateResponse(
        request=request,
        name="run_detail.html",
        context={"page": "runs", "run": dict(row)},
    )


# ---------------------------------------------------------------------------
# GET /push-logs — 推送日志
# ---------------------------------------------------------------------------


@router.get("/push-logs")
async def push_logs_page(request: Request, _user: bool = Depends(require_ui_session)):
    """推送记录全列表。"""
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        cur = await db.execute(
            "SELECT id, run_id, provider_name, group_id, keys_count, "
            "added_count, ignored_count, status, pushed_at "
            "FROM push_logs ORDER BY pushed_at DESC"
        )
        rows = [dict(row) for row in await cur.fetchall()]
    finally:
        await db.close()

    return templates.TemplateResponse(
        request=request,
        name="push_logs.html",
        context={"page": "push-logs", "logs": rows},
    )


# ---------------------------------------------------------------------------
# GET /push-config — 推送配置（gpt-load 实例 + provider→分组映射）
# ---------------------------------------------------------------------------


@router.get("/push-config")
async def push_config_page(request: Request, _user: bool = Depends(require_ui_session)):
    """推送目标配置页：gpt-load 实例列表 + 每个 provider 的目标分组映射。"""
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        cur = await db.execute(
            "SELECT id, name, base_url, enabled, created_at "
            "FROM gpt_load_config ORDER BY id"
        )
        configs = [dict(row) for row in await cur.fetchall()]

        cur = await db.execute(
            "SELECT id, provider_name, gpt_load_config_id, group_id, group_name, "
            "max_size, enabled FROM provider_group_mapping ORDER BY provider_name"
        )
        mappings = [dict(row) for row in await cur.fetchall()]
    finally:
        await db.close()

    # All supported task types: union of tasks declared in example configs
    # and tasks that already have a mapping (page remains fully configurable).
    providers = _scan_supported_providers()
    mapped = {m["provider_name"] for m in mappings}
    for extra in sorted(mapped - set(providers)):
        providers.append(extra)

    return templates.TemplateResponse(
        request=request,
        name="push_config.html",
        context={
            "page": "push-config",
            "configs": configs,
            "mappings": mappings,
            "providers": providers,
        },
    )


# ---------------------------------------------------------------------------
# GET /login — 登录页（无需认证）
# ---------------------------------------------------------------------------


@router.get("/login")
async def login_page(request: Request):
    """登录表单页：POST 到 /api/auth/login。"""
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"page": "login"},
    )


# ---------------------------------------------------------------------------
# GET /logout — 退出登录（清除会话 cookie）
# ---------------------------------------------------------------------------


@router.get("/logout")
async def logout():
    """Clear the session cookie and redirect to the login page."""
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp