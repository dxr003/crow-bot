"""L0 Event Ledger — 事件账本基础设施。

定位（乌鸦定稿 2026-04-21）：
- 所有模块共用的事件记录 + 查询 + 成本结算底座
- 与 shared/core.py 并列：shared/core.py 是代码层 L0，ledger 是事件层 L0
- 强制 import 使用，不走 config 开关

核心概念：
- event(name, payload, trace_id=, parent_trace_id=, cost_usd=) 写一条账本
- trace_id：信号→派发→执行 一单到底，场景 B 回溯靠它
- parent_trace_id：跨进程/跨模块父子链，未来聚合查询用
- cost_usd：每笔付费调用记账，Nansen/OpenRouter 接入前必备

物理存储：/root/logs/{system,exec,signal,dialog,external}/*.jsonl（JSONL + 轮转）
物理路径保留 /root/logs/，因为 journalctl/tail/jq 等工具已经习惯这个位置。
"""
from .core import (
    Ledger,
    get_ledger,
    new_trace_id,
    set_trace_id,
    current_trace_id,
    now_iso,
)
from .external import log_external_call
from .redact import scrub

__all__ = [
    "Ledger",
    "get_ledger",
    "new_trace_id",
    "set_trace_id",
    "current_trace_id",
    "now_iso",
    "log_external_call",
    "scrub",
]
