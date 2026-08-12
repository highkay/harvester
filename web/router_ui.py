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
    """调度配置全列表。"""
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

    return templates.TemplateResponse(
        request=request,
        name="schedule.html",
        context={"page": "schedule", "schedules": rows},
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