"""统一日志核心。

设计约束（乌鸦定稿 2026-04-21）：
- 跨域共用根目录：/root/logs/{system,exec,signal,dialog,external}/
- 格式：JSON Lines，一行一事件
- 时间戳：ISO8601 + 08:00（北京时间）
- 统一字段：ts / trace_id / level / module / event / payload
- 轮转：RotatingFileHandler，默认 10MB × 5 份，可按业务覆写
- trace_id：signal 生成 → dispatch 传递 → executor 落盘 → guardian 关联
  * 进程内用 ContextVar 传递；跨进程用显式参数传递

最小用法：
    from logs.lib.logkit import get_logger, new_trace_id, set_trace_id

    log = get_logger("exec", event_file="orders")
    set_trace_id(new_trace_id())
    log.event("open_market", {"symbol": "BTCUSDT", "side": "BUY", "margin": 100})
"""
from __future__ import annotations

import json
import logging
import secrets
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .redact import scrub

LOGS_ROOT = Path("/root/logs")
BEIJING_TZ = timezone(timedelta(hours=8))

# 业务域白名单（对应 /root/logs/ 下的 5 个子目录）
DOMAINS = {"system", "exec", "signal", "dialog", "external"}

# 轮转默认值（按业务可覆写）
DEFAULT_MAX_BYTES = 10 * 1024 * 1024   # 10 MB
DEFAULT_BACKUP = 5
EXEC_MAX_BYTES = 15 * 1024 * 1024      # 15 MB（关键执行日志更大）
EXEC_BACKUP = 10

# 进程内 trace_id 透传
_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")


def now_iso() -> str:
    """北京时间 ISO8601：2026-04-21T14:15:24+08:00"""
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def new_trace_id() -> str:
    """8 位 hex trace_id。用 secrets 生成，冲突概率忽略。"""
    return secrets.token_hex(4)


def set_trace_id(tid: str) -> None:
    """设置当前进程内后续日志的 trace_id。"""
    _trace_id_var.set(tid)


def current_trace_id() -> str:
    """取当前 trace_id；未设置返回空串。"""
    return _trace_id_var.get()


class _JsonlFormatter(logging.Formatter):
    """把 LogRecord 编码成一行 JSON。"""

    def __init__(self, domain: str, module_name: str) -> None:
        super().__init__()
        self.domain = domain
        self.module_name = module_name

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "event_payload", None)
        event = getattr(record, "event_name", record.msg)
        trace = getattr(record, "trace_id", None) or current_trace_id() or ""
        obj = {
            "ts": now_iso(),
            "trace_id": trace,
            "level": record.levelname,
            "module": self.module_name,
            "event": event,
            "payload": scrub(payload) if payload is not None else {},
        }
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class EventLogger:
    """薄封装：log.event(name, payload) 一次调用落一行 JSONL。

    仍暴露标准 .info/.warning/.error 接口兼容老代码，但字段会打包到 payload。
    """

    def __init__(self, logger: logging.Logger, module_name: str) -> None:
        self._logger = logger
        self._module_name = module_name

    def event(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        level: str = "INFO",
        trace_id: str | None = None,
    ) -> None:
        extra = {
            "event_name": name,
            "event_payload": payload or {},
        }
        if trace_id:
            extra["trace_id"] = trace_id
        self._logger.log(logging._nameToLevel.get(level, logging.INFO), name, extra=extra)

    def info(self, msg: str, **kw: Any) -> None:
        self.event("_text", {"text": msg, **kw}, level="INFO")

    def warning(self, msg: str, **kw: Any) -> None:
        self.event("_text", {"text": msg, **kw}, level="WARNING")

    def error(self, msg: str, **kw: Any) -> None:
        self.event("_text", {"text": msg, **kw}, level="ERROR")


def get_logger(
    domain: str,
    event_file: str,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> EventLogger:
    """拿一个 EventLogger。

    Args:
        domain: system / exec / signal / dialog / external
        event_file: 文件名（不含扩展名），e.g. "orders" 落到 exec/orders.jsonl
        max_bytes / backup_count: 轮转参数，默认按 domain 自动选

    同一 (domain, event_file) 多次调用返回同一 logger 实例，handler 不重复添加。
    """
    if domain not in DOMAINS:
        raise ValueError(f"domain 必须是 {DOMAINS}，收到 {domain!r}")

    # 轮转参数
    if domain == "exec":
        max_bytes = max_bytes or EXEC_MAX_BYTES
        backup_count = backup_count or EXEC_BACKUP
    else:
        max_bytes = max_bytes or DEFAULT_MAX_BYTES
        backup_count = backup_count or DEFAULT_BACKUP

    log_path = LOGS_ROOT / domain / f"{event_file}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger_name = f"logs.{domain}.{event_file}"
    logger = logging.getLogger(logger_name)

    # 幂等：只添加一次 handler
    already_wired = any(
        isinstance(h, RotatingFileHandler) and getattr(h, "_logkit_path", None) == str(log_path)
        for h in logger.handlers
    )
    if not already_wired:
        handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler._logkit_path = str(log_path)  # type: ignore[attr-defined]
        handler.setFormatter(_JsonlFormatter(domain=domain, module_name=event_file))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False  # 不冒泡到 root，避免重复输出

    return EventLogger(logger, module_name=event_file)
