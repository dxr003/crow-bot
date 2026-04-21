"""L0 事件账本核心。

接口定型（乌鸦定稿 2026-04-21）：
    from ledger import get_ledger, new_trace_id, set_trace_id

    lg = get_ledger("exec", "orders")
    set_trace_id(new_trace_id())
    lg.event("open_market",
             {"symbol": "BTCUSDT", "side": "BUY", "margin": 100},
             parent_trace_id="sig_abc123",
             cost_usd=0.0)

物理存储：/root/logs/{domain}/{event_file}.jsonl
schema：
    ts            ISO8601 + 08:00
    trace_id      8 hex，进程内 ContextVar 自动传
    parent_trace_id  可选，跨模块父子链
    level         INFO/WARNING/ERROR
    module        event_file 名
    event         事件名（动词短语）
    cost_usd      可选，付费调用记账
    payload       事件具体字段（已经过脱敏）
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

DOMAINS = {"system", "exec", "signal", "dialog", "external"}

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP = 5
EXEC_MAX_BYTES = 15 * 1024 * 1024
EXEC_BACKUP = 10

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")


def now_iso() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def new_trace_id() -> str:
    return secrets.token_hex(4)


def set_trace_id(tid: str) -> None:
    _trace_id_var.set(tid)


def current_trace_id() -> str:
    return _trace_id_var.get()


class _JsonlFormatter(logging.Formatter):
    def __init__(self, domain: str, module_name: str) -> None:
        super().__init__()
        self.domain = domain
        self.module_name = module_name

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "event_payload", None)
        event = getattr(record, "event_name", record.msg)
        trace = getattr(record, "trace_id", None) or current_trace_id() or ""
        parent = getattr(record, "parent_trace_id", None) or ""
        cost = getattr(record, "cost_usd", None)

        obj: dict[str, Any] = {
            "ts": now_iso(),
            "trace_id": trace,
            "level": record.levelname,
            "module": self.module_name,
            "event": event,
        }
        if parent:
            obj["parent_trace_id"] = parent
        if cost is not None:
            obj["cost_usd"] = cost
        obj["payload"] = scrub(payload) if payload is not None else {}
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class Ledger:
    """账本写入器。薄封装，一次 .event() 落一行。"""

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
        parent_trace_id: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        extra: dict[str, Any] = {
            "event_name": name,
            "event_payload": payload or {},
        }
        if trace_id:
            extra["trace_id"] = trace_id
        if parent_trace_id:
            extra["parent_trace_id"] = parent_trace_id
        if cost_usd is not None:
            extra["cost_usd"] = cost_usd
        self._logger.log(
            logging._nameToLevel.get(level, logging.INFO), name, extra=extra
        )

    def info(self, msg: str, **kw: Any) -> None:
        self.event("_text", {"text": msg, **kw}, level="INFO")

    def warning(self, msg: str, **kw: Any) -> None:
        self.event("_text", {"text": msg, **kw}, level="WARNING")

    def error(self, msg: str, **kw: Any) -> None:
        self.event("_text", {"text": msg, **kw}, level="ERROR")


def get_ledger(
    domain: str,
    event_file: str,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> Ledger:
    """拿一个 Ledger。同一 (domain, event_file) 返回单例，handler 不重复添加。

    Args:
        domain: system / exec / signal / dialog / external
        event_file: 文件名（不含扩展名）
    """
    if domain not in DOMAINS:
        raise ValueError(f"domain 必须是 {DOMAINS}，收到 {domain!r}")

    if domain == "exec":
        max_bytes = max_bytes or EXEC_MAX_BYTES
        backup_count = backup_count or EXEC_BACKUP
    else:
        max_bytes = max_bytes or DEFAULT_MAX_BYTES
        backup_count = backup_count or DEFAULT_BACKUP

    log_path = LOGS_ROOT / domain / f"{event_file}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger_name = f"ledger.{domain}.{event_file}"
    logger = logging.getLogger(logger_name)

    already_wired = any(
        isinstance(h, RotatingFileHandler)
        and getattr(h, "_ledger_path", None) == str(log_path)
        for h in logger.handlers
    )
    if not already_wired:
        handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler._ledger_path = str(log_path)  # type: ignore[attr-defined]
        handler.setFormatter(_JsonlFormatter(domain=domain, module_name=event_file))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

    return Ledger(logger, module_name=event_file)
