"""
exec_log.py — multi/executor 动作日志（jsonl 追加，不读不锁）

设计目标：
- 每次 executor 公开方法调用都留痕：角色/账户/动作/参数/返回/耗时/异常
- jsonl 格式天然 append-safe，多线程并发 fan-out 不冲突（OS open() O_APPEND 原子）
- 单文件按行尾 newline append，无并发损坏
- 提供 read_recent / format_for_tg 给 bot 和后续审查脚本用

字段约定（每行一个 JSON 对象）：
  ts        : float epoch
  dt        : "MM-DD HH:MM:SS"
  role      : "玄玄" / "天天" / "大猫" / "策略:bull_trailing" 等
  account   : 规范化后账户名（"币安1" / "币安2"...）
  action    : "open_market" / "close_market" / "place_stop_loss" 等
  symbol    : 交易对（无关动作可省）
  args      : 调用参数摘要（脱敏后）
  ok        : True/False
  result    : 成功摘要（orderId / qty / price / no_position 等）
  error     : 异常信息（失败时）
  ms        : 耗时毫秒
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_FILE = Path("/root/maomao/data/exec_log.jsonl")
MAX_BYTES = 5 * 1024 * 1024  # 5MB 触发滚动
ROTATE_KEEP = 3              # 保留最近 3 个滚动文件


def _rotate_if_needed():
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_BYTES:
            for i in range(ROTATE_KEEP - 1, 0, -1):
                src = LOG_FILE.with_suffix(f".jsonl.{i}")
                dst = LOG_FILE.with_suffix(f".jsonl.{i + 1}")
                if src.exists():
                    src.replace(dst)
            LOG_FILE.replace(LOG_FILE.with_suffix(".jsonl.1"))
    except Exception as e:
        logger.warning(f"[exec_log] rotate 失败: {e}")


def _write(entry: dict):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[exec_log] 写入失败: {e}")


def _summarize_args(action: str, args: tuple, kwargs: dict) -> dict:
    """从位置参数+关键字提取摘要，脱敏（无 secret，但可能太长截断）。"""
    # executor 公开方法签名首位都是 (role, account, ...)；前两位单独提取
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


def _safe(v):
    if isinstance(v, (int, float, bool, type(None))):
        return v
    s = str(v)
    return s if len(s) < 200 else s[:200] + "..."


def _summarize_result(result):
    """成功返回的关键字段摘要；失败返回 error。"""
    if not isinstance(result, dict):
        return {"raw": _safe(result)}
    keys = ["ok", "orderId", "qty", "price", "side", "leverage",
            "margin", "notional", "hedge", "no_position",
            "closed", "errors", "type", "tranId", "amount",
            "stopPrice", "tpPrice"]
    out = {k: result[k] for k in keys if k in result}
    return out


def log_call(action_name: str | None = None):
    """装饰器：包 executor 公开方法，自动写日志。

    action_name 默认取被装饰函数 __name__。
    role 从 args[0] 取（executor 约定）。
    account 从 args[1] 取，但日志写"未规范化"原值，因为 executor 内部会自己 resolve。
    """
    def deco(fn):
        name = action_name or fn.__name__

        @wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.time()
            entry = {
                "ts": t0,
                "dt": datetime.fromtimestamp(t0).strftime("%m-%d %H:%M:%S"),
                "action": name,
                "role": args[0] if args else kwargs.get("role"),
                "account": args[1] if len(args) >= 2 else kwargs.get("account"),
                "args": _summarize_args(name, args, kwargs),
            }
            # 提取 symbol（如有）
            sym = kwargs.get("symbol")
            if sym is None and len(args) >= 3 and isinstance(args[2], str):
                sym = args[2]
            if sym:
                entry["symbol"] = sym
            try:
                result = fn(*args, **kwargs)
                entry["ms"] = int((time.time() - t0) * 1000)
                if isinstance(result, dict):
                    entry["ok"] = bool(result.get("ok"))
                    if result.get("error"):
                        entry["error"] = _safe(result["error"])
                    entry["result"] = _summarize_result(result)
                else:
                    entry["ok"] = True
                    entry["result"] = {"raw": _safe(result)}
                _write(entry)
                return result
            except Exception as e:
                entry["ms"] = int((time.time() - t0) * 1000)
                entry["ok"] = False
                entry["error"] = _safe(e)
                entry["exception_type"] = type(e).__name__
                _write(entry)
                raise

        return wrapper
    return deco


# ──────────────────────────────────────────
# 读取 / 展示
# ──────────────────────────────────────────

def read_recent(limit: int = 50, action_filter: str | None = None,
                account_filter: str | None = None) -> list[dict]:
    """倒序读最近 limit 条（仅当前 jsonl，不翻 .1/.2）。"""
    if not LOG_FILE.exists():
        return []
    out = []
    try:
        with LOG_FILE.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if action_filter and e.get("action") != action_filter:
                continue
            if account_filter and e.get("account") not in (account_filter, None):
                continue
            out.append(e)
            if len(out) >= limit:
                break
    except Exception as ex:
        logger.warning(f"[exec_log] 读取失败: {ex}")
    return out


def format_for_tg(entries: list[dict]) -> str:
    if not entries:
        return "📭 无动作记录"
    lines = []
    for e in entries:
        icon = "✅" if e.get("ok") else "❌"
        dt = e.get("dt", "")
        action = e.get("action", "?")
        role = e.get("role", "?")
        account = e.get("account") or "-"
        symbol = e.get("symbol") or ""
        ms = e.get("ms", 0)
        head = f"{icon} <b>{dt}</b> {action} {symbol}"
        body = f"  {role}@{account} ({ms}ms)"
        if e.get("error"):
            body += f"\n  err: {e['error'][:120]}"
        else:
            r = e.get("result", {})
            tail_bits = []
            for k in ("orderId", "qty", "price", "leverage", "no_position"):
                if k in r:
                    tail_bits.append(f"{k}={r[k]}")
            if tail_bits:
                body += "\n  " + " ".join(tail_bits)
        lines.append(head + "\n" + body)
    return "\n\n".join(lines)


if __name__ == "__main__":
    # 自检：写一条测试 + 读回
    _write({
        "ts": time.time(),
        "dt": datetime.now().strftime("%m-%d %H:%M:%S"),
        "action": "_self_test",
        "role": "test",
        "account": "test",
        "ok": True,
        "result": {"msg": "exec_log 自检通过"},
        "ms": 0,
    })
    print(format_for_tg(read_recent(5)))
