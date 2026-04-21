"""乌鸦团队统一日志核心模块。

入口：
    from logs.lib.logkit import get_logger, new_trace_id, set_trace_id
    from logs.lib.redact import scrub

详见 /root/logs/README.md（待写）和 /root/maomao/trader/docs/logging_redesign_plan.md。
"""
from .logkit import get_logger, new_trace_id, set_trace_id, current_trace_id
from .redact import scrub

__all__ = ["get_logger", "new_trace_id", "set_trace_id", "current_trace_id", "scrub"]
