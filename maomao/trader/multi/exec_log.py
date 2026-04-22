"""
exec_log.py — multi/executor 动作日志

Phase A（2026-04-21）切换到 /root/logs/ 统一日志骨架：
  旧写入：/root/maomao/data/exec_log.jsonl（5MB × 3，手写 JSONL）
  新写入：/root/logs/exec/orders.jsonl（15MB × 10，logkit RotatingFileHandler）

统一 schema：ts(ISO8601+08) / trace_id(8hex) / level / module / event / payload
payload 内含老字段（role/account/symbol/args/ok/result/error/ms）。

trace_id 从 ContextVar 读；为空时 executor 首发调用处自动生成。
上游 dispatch/信号推送后续会显式 set_trace_id（Phase A 后半 + B）。

对外接口不变：
  log_call(action_name=None)  装饰器
  read_recent(...) / format_for_tg(...)  向后兼容，字段映射老 schema

老日志文件保留不动，仅停写。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from functools import wraps
from pathlib import Path

# 让 /root/logs 能被 import（maomao.service 的 WorkingDirectory=/root/maomao，默认无 /root 在 path）
if "/root" not in sys.path:
    sys.path.insert(0, "/root")

from ledger import get_ledger, new_trace_id, set_trace_id, current_trace_id

logger_std = logging.getLogger(__name__)

# 新日志路径（只读用，写入走 ledger）
NEW_LOG_FILE = Path("/root/logs/exec/orders.jsonl")

# L0 账本单例
_exec_logger = get_ledger("exec", "orders")


def _safe(v):
    if isinstance(v, (int, float, bool, type(None))):
        return v
    s = str(v)
    return s if len(s) < 200 else s[:200] + "..."


def _summarize_args(args: tuple, kwargs: dict) -> dict:
    out = {}
    try:
        if len(args) >= 1:
            out["_role_arg"] = args[0]
        if len(args) >= 2:
            out["_acc_arg"] = args[1]
        for i, v in enumerate(args[2:], start=2):
            out[f"a{i}"] = _safe(v)
        for k, v in kwargs.items():
            out[k] = _safe(v)
    except Exception:
        pass
    return out


def _summarize_result(result):
    if not isinstance(result, dict):
        return {"raw": _safe(result)}
    keys = ["ok", "orderId", "algoId", "qty", "price", "side", "leverage",
            "margin", "notional", "hedge", "no_position",
            "closed", "errors", "type", "tranId", "amount",
            "stopPrice", "tpPrice"]
    return {k: result[k] for k in keys if k in result}


def log_call(action_name: str | None = None):
    """装饰器：包 executor 公开方法，写 /root/logs/exec/orders.jsonl。

    trace_id 优先读 ContextVar，未设则自动生成（executor 首发）。
    """
    def deco(fn):
        name = action_name or fn.__name__

        @wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.time()

            tid = current_trace_id()
            if not tid:
                tid = new_trace_id()
                set_trace_id(tid)

            role = args[0] if args else kwargs.get("role")
            account = args[1] if len(args) >= 2 else kwargs.get("account")
            sym = kwargs.get("symbol")
            if sym is None and len(args) >= 3 and isinstance(args[2], str):
                sym = args[2]

            payload_head = {
                "role": role,
                "account": account,
                "symbol": sym,
                "args": _summarize_args(args, kwargs),
            }

            try:
                result = fn(*args, **kwargs)
                elapsed_ms = int((time.time() - t0) * 1000)
                payload = {**payload_head, "ms": elapsed_ms}
                if isinstance(result, dict):
                    # 动作类返 {ok:True/False}；查询类不带 ok —— 按"显式 False 或有 error 才算失败"判
                    if "ok" in result:
                        payload["ok"] = bool(result["ok"])
                    else:
                        payload["ok"] = not bool(result.get("error"))
                    if result.get("error"):
                        payload["error"] = _safe(result["error"])
                    payload["result"] = _summarize_result(result)
                else:
                    payload["ok"] = True
                    payload["result"] = {"raw": _safe(result)}
                _exec_logger.event(name, payload, trace_id=tid)
                return result
            except Exception as e:
                elapsed_ms = int((time.time() - t0) * 1000)
                _exec_logger.event(
                    name,
                    {
                        **payload_head,
                        "ms": elapsed_ms,
                        "ok": False,
                        "error": _safe(str(e)),
                        "exception_type": type(e).__name__,
                    },
                    trace_id=tid,
                    level="ERROR",
                )
                raise

        return wrapper
    return deco


# ──────────────────────────────────────────
# 读取 / 展示（新 schema 映射回老字段，调用方无感）
# ──────────────────────────────────────────

def read_recent(limit: int = 50, action_filter: str | None = None,
                account_filter: str | None = None) -> list[dict]:
    """倒序读最近 limit 条（仅当前 orders.jsonl，不翻轮转备份）。

    字段映射：event→action, payload.role→role, payload.account→account 等。
    """
    if not NEW_LOG_FILE.exists():
        return []
    out = []
    try:
        with NEW_LOG_FILE.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            p = e.get("payload") or {}
            view = {
                "ts": e.get("ts"),
                "dt": e.get("ts", ""),
                "trace_id": e.get("trace_id"),
                "action": e.get("event"),
                "role": p.get("role"),
                "account": p.get("account"),
                "symbol": p.get("symbol"),
                "args": p.get("args"),
                "ok": p.get("ok"),
                "error": p.get("error"),
                "result": p.get("result") or {},
                "ms": p.get("ms"),
            }
            if action_filter and view["action"] != action_filter:
                continue
            if account_filter and view["account"] not in (account_filter, None):
                continue
            out.append(view)
            if len(out) >= limit:
                break
    except Exception as ex:
        logger_std.warning(f"[exec_log] 读取失败: {ex}")
    return out


def format_for_tg(entries: list[dict]) -> str:
    if not entries:
        return "📭 无动作记录"
    lines = []
    for e in entries:
        icon = "✅" if e.get("ok") else "❌"
        ts = e.get("dt") or ""
        # ISO8601 → "MM-DD HH:MM:SS" 展示
        if isinstance(ts, str) and "T" in ts:
            try:
                date_part, time_part = ts.split("T", 1)
                ts = f"{date_part[5:10]} {time_part[:8]}"
            except Exception:
                pass
        action = e.get("action", "?")
        role = e.get("role", "?")
        account = e.get("account") or "-"
        symbol = e.get("symbol") or ""
        ms = e.get("ms", 0)
        tid = e.get("trace_id") or ""
        head = f"{icon} <b>{ts}</b> {action} {symbol}"
        body = f"  {role}@{account} ({ms}ms) tid={tid}"
        if e.get("error"):
            body += f"\n  err: {str(e['error'])[:120]}"
        else:
            r = e.get("result") or {}
            tail_bits = []
            for k in ("orderId", "qty", "price", "leverage", "no_position"):
                if k in r:
                    tail_bits.append(f"{k}={r[k]}")
            if tail_bits:
                body += "\n  " + " ".join(tail_bits)
        lines.append(head + "\n" + body)
    return "\n\n".join(lines)


if __name__ == "__main__":
    print(format_for_tg(read_recent(5)))
