"""外部 API 调用账本（动作版，不记成本）。

对齐 conventions §6.6：三态 api_call_started / api_call_completed / api_call_failed。
所有函数自动从 ContextVar 继承 trace_id（可显式覆盖）。

典型用法（analyzer/新闻/AI 调用点）：

    from ledger import (
        log_api_call_started, log_api_call_completed, log_api_call_failed,
    )

    t0 = time.time()
    log_api_call_started(
        provider="tavily",
        endpoint="/search",
        method="POST",
        request_summary={"query_hash": _hash(q)},
    )
    try:
        resp = requests.post(url, json=body, timeout=10)
        log_api_call_completed(
            provider="tavily",
            endpoint="/search",
            status_code=resp.status_code,
            duration_ms=int((time.time() - t0) * 1000),
            response_summary={"result_count": len(resp.json().get("results", []))},
        )
    except requests.Timeout:
        log_api_call_failed(
            provider="tavily", endpoint="/search",
            error_msg="timeout", result="timeout",
            duration_ms=int((time.time() - t0) * 1000),
        )
    except Exception as e:
        log_api_call_failed(
            provider="tavily", endpoint="/search",
            error_msg=str(e)[:200],
        )

设计原则（乌鸦 2026-04-21 定）：
- 只记"动作发生了"，不记成本（cost_usd / credit / token 一律不落）
- request_summary / response_summary 是结构化摘要（数量、hash），不是完整 body
- trace_id 强制从 ContextVar 继承，调用方无需手动传（除非跨进程）
"""
from __future__ import annotations

from typing import Any

from .core import Ledger, get_ledger, current_trace_id

_ext_ledger: Ledger = get_ledger("external", "api_calls")


def _resolve_tid(trace_id: str | None) -> str:
    return trace_id or current_trace_id() or ""


def log_api_call_started(
    *,
    provider: str,
    endpoint: str,
    method: str = "POST",
    request_summary: dict[str, Any] | None = None,
    trace_id: str | None = None,
    parent_trace_id: str | None = None,
) -> None:
    body: dict[str, Any] = {
        "provider": provider,
        "endpoint": endpoint,
        "method": method,
    }
    if request_summary:
        body["request_summary"] = request_summary
    _ext_ledger.event(
        "api_call_started",
        body,
        actor=provider,
        target=provider,
        result="pending",
        trace_id=_resolve_tid(trace_id),
        parent_trace_id=parent_trace_id,
        level="INFO",
    )


def log_api_call_completed(
    *,
    provider: str,
    endpoint: str,
    status_code: int,
    duration_ms: int,
    method: str = "POST",
    response_summary: dict[str, Any] | None = None,
    result: str = "success",
    trace_id: str | None = None,
    parent_trace_id: str | None = None,
) -> None:
    if result not in ("success", "partial"):
        raise ValueError(
            f"completed 事件 result 只允许 success/partial，收到 {result!r}"
        )
    body: dict[str, Any] = {
        "provider": provider,
        "endpoint": endpoint,
        "method": method,
        "status_code": status_code,
        "duration_ms": duration_ms,
    }
    if response_summary:
        body["response_summary"] = response_summary
    _ext_ledger.event(
        "api_call_completed",
        body,
        actor=provider,
        target=provider,
        result=result,
        trace_id=_resolve_tid(trace_id),
        parent_trace_id=parent_trace_id,
        level="INFO",
    )


def log_api_call_failed(
    *,
    provider: str,
    endpoint: str,
    error_msg: str,
    method: str = "POST",
    status_code: int | None = None,
    duration_ms: int | None = None,
    result: str = "failed",
    related_files: list[str] | None = None,
    trace_id: str | None = None,
    parent_trace_id: str | None = None,
) -> None:
    if result not in ("failed", "timeout", "rate_limited"):
        raise ValueError(
            f"failed 事件 result 只允许 failed/timeout/rate_limited，收到 {result!r}"
        )
    body: dict[str, Any] = {
        "provider": provider,
        "endpoint": endpoint,
        "method": method,
    }
    if status_code is not None:
        body["status_code"] = status_code
    if duration_ms is not None:
        body["duration_ms"] = duration_ms
    _ext_ledger.event(
        "api_call_failed",
        body,
        actor=provider,
        target=provider,
        result=result,
        trace_id=_resolve_tid(trace_id),
        parent_trace_id=parent_trace_id,
        error_msg=error_msg,
        related_files=related_files,
        level="ERROR",
    )
