"""L0 事件账本核心。

接口定型（2026-04-21 v2.0 动作账本）：

    from ledger import get_ledger, new_trace_id, set_trace_id

    lg = get_ledger("exec", "orders")
    set_trace_id(new_trace_id())
    lg.event(
        "order_placed",
        {"account":"币安1","side":"BUY","qty":0.001,"order_type":"MARKET"},
        actor="executor",
        target="BTCUSDT",
        result="pending",
    )

物理存储：/root/logs/{domain}/{event_file}.jsonl
schema（见 ledger/ledger_conventions.md §4）：
    必选：ts / trace_id / level / actor / event_type / target / result / payload
    可选：parent_trace_id / error_msg / related_files
    禁用：cost_usd / provider / model / token_usage / credit_cost / latency_ms

兼容：历史 JSONL 可能含 cost_usd / module 字段，读取方按 §10 双字段兼容。
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

DOMAINS = {"system", "exec", "signal", "risk", "dialog", "external", "trace"}

_DEFAULT_CAPS = {
    "exec":     (15 * 1024 * 1024, 10),
    "signal":   (10 * 1024 * 1024, 5),
    "risk":     (10 * 1024 * 1024, 5),
    "system":   (10 * 1024 * 1024, 5),
    "dialog":   (10 * 1024 * 1024, 5),
    "external": (10 * 1024 * 1024, 5),
    "trace":    (20 * 1024 * 1024, 10),
}

# 7 态 enum，见 conventions §4.1 v1.1
VALID_RESULTS = {
    "success", "failed", "timeout", "rate_limited",
    "partial", "pending", "n-a",
}

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
        event_type = getattr(record, "event_name", record.msg)
        trace = getattr(record, "trace_id", None) or current_trace_id() or ""
        parent = getattr(record, "parent_trace_id", None) or ""
        actor = getattr(record, "actor", None) or self.module_name
        target = getattr(record, "target", "") or ""
        result = getattr(record, "result", "n-a") or "n-a"
        error_msg = getattr(record, "error_msg", None)
        related_files = getattr(record, "related_files", None)
        payload = getattr(record, "event_payload", None)

        obj: dict[str, Any] = {
            "ts": now_iso(),
            "trace_id": trace,
            "level": record.levelname,
            "actor": actor,
            "event_type": event_type,
            "target": target,
            "result": result,
            "payload": scrub(payload) if payload is not None else {},
        }
        if parent:
            obj["parent_trace_id"] = parent
        if error_msg:
            obj["error_msg"] = error_msg
        if related_files:
            obj["related_files"] = related_files
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
        actor: str | None = None,
        target: str = "",
        result: str = "n-a",
        trace_id: str | None = None,
        parent_trace_id: str | None = None,
        error_msg: str | None = None,
        related_files: list[str] | None = None,
    ) -> None:
        if result not in VALID_RESULTS:
            raise ValueError(
                f"result 必须是 {VALID_RESULTS}，收到 {result!r}"
            )
        extra: dict[str, Any] = {
            "event_name": name,
            "event_payload": payload or {},
            "target": target,
            "result": result,
        }
        if actor:
            extra["actor"] = actor
        if trace_id:
            extra["trace_id"] = trace_id
        if parent_trace_id:
            extra["parent_trace_id"] = parent_trace_id
        if error_msg:
            extra["error_msg"] = error_msg
        if related_files:
            extra["related_files"] = related_files
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
    """拿一个 Ledger。同一 (domain, event_file) 返回同一 handler，不重复添加。

    Args:
        domain: system / exec / signal / risk / dialog / external / trace
        event_file: 文件名（不含扩展名）
    """
    if domain not in DOMAINS:
        raise ValueError(f"domain 必须是 {DOMAINS}，收到 {domain!r}")

    default_bytes, default_backup = _DEFAULT_CAPS[domain]
    max_bytes = max_bytes or default_bytes
    backup_count = backup_count or default_backup

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
