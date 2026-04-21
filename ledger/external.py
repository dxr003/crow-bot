"""付费/外部 API 调用记账。

用法（analyzer / 新闻抓取 / AI 调用点接入）：

    from ledger import log_external_call

    t0 = time.time()
    resp = requests.post(url, ...)
    log_external_call(
        provider="anthropic",
        endpoint="/v1/messages",
        model="claude-haiku-4-5",
        status=resp.status_code,
        duration_ms=int((time.time() - t0) * 1000),
        cost_usd=calc_anthropic_cost(usage),   # 调用方算好传进来
        payload={"prompt_tokens": usage["input_tokens"],
                 "output_tokens": usage["output_tokens"]},
    )

设计：
- 只负责"落一条账本"，不负责算成本（价格表交调用方维护）
- trace_id 自动从 ContextVar 继承（信号生成 → 分析调用 → Haiku 调用 同一链）
- payload 里的敏感字段（api_key/tg_token）由 scrub 自动脱敏
"""
from __future__ import annotations

from typing import Any

from .core import get_ledger

_ext_ledger = get_ledger("external", "api_calls")


def log_external_call(
    *,
    provider: str,
    endpoint: str,
    model: str | None = None,
    status: int | str | None = None,
    duration_ms: int | None = None,
    cost_usd: float | None = None,
    payload: dict[str, Any] | None = None,
    trace_id: str | None = None,
    parent_trace_id: str | None = None,
    error: str | None = None,
) -> None:
    body: dict[str, Any] = {
        "provider": provider,
        "endpoint": endpoint,
    }
    if model:
        body["model"] = model
    if status is not None:
        body["status"] = status
    if duration_ms is not None:
        body["duration_ms"] = duration_ms
    if payload:
        body["payload"] = payload
    if error:
        body["error"] = error

    _ext_ledger.event(
        "call_failed" if error else "call",
        body,
        trace_id=trace_id,
        parent_trace_id=parent_trace_id,
        cost_usd=cost_usd,
        level="ERROR" if error else "INFO",
    )
