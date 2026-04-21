"""L0 Event Ledger — 动作账本基础设施。

定位（乌鸦 2026-04-21 v2.0 定稿）：
- 所有模块共用的动作记录 + 查询底座
- 只记动作，不记成本（cost_usd / token_usage / credit_cost 一律不落）
- 强制 import 使用，不走 config 开关

核心概念：
- event(name, payload, *, actor, target, result, error_msg, related_files) 写一条
- trace_id：信号→派发→执行 一单到底，Q1 动作追溯 / Q3 信号回溯靠它
- parent_trace_id：跨进程/跨模块父子链，未来聚合查询用
- related_files：金路径 + failed 自动采集，Q2「改 A 忘 B」防御靠它

约束见 `/root/ledger/ledger_conventions.md`（改前需乌鸦明批）。

物理存储：/root/logs/{exec,signal,risk,system,dialog,external,trace}/*.jsonl
"""
from .core import (
    Ledger,
    get_ledger,
    new_trace_id,
    set_trace_id,
    current_trace_id,
    now_iso,
)
from .external import (
    log_api_call_started,
    log_api_call_completed,
    log_api_call_failed,
)
from .redact import scrub


def log_order_placed(
    *,
    symbol: str,
    account: str,
    side: str,
    qty: float,
    order_type: str = "MARKET",
    position_side: str | None = None,
    result: str = "success",
    trace_id: str | None = None,
) -> str:
    """最小闭环 helper：往 exec/orders.jsonl 写一条 order_placed。返回 trace_id。"""
    tid = trace_id or current_trace_id() or new_trace_id()
    payload = {"account": account, "side": side, "qty": qty, "order_type": order_type}
    if position_side:
        payload["position_side"] = position_side
    get_ledger("exec", "orders").event(
        "order_placed",
        payload,
        actor="executor",
        target=symbol,
        result=result,
        trace_id=tid,
    )
    return tid


__all__ = [
    "Ledger",
    "get_ledger",
    "new_trace_id",
    "set_trace_id",
    "current_trace_id",
    "now_iso",
    "log_api_call_started",
    "log_api_call_completed",
    "log_api_call_failed",
    "log_order_placed",
    "scrub",
]
