"""
交易执行日志 — 记录所有开/平/改/撤/报错
保留策略：最近 100 条 且 7 天内
"""
import json, time, re
from datetime import datetime
from pathlib import Path

LOG_FILE  = Path("/root/maomao/data/trade_log.json")
MAX_ITEMS = 100
MAX_DAYS  = 7

def _load() -> list:
    try:
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def _save(entries: list):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

def _rotate(entries: list) -> list:
    cutoff = time.time() - MAX_DAYS * 86400
    entries = [e for e in entries if e.get("ts", 0) >= cutoff]
    return entries[-MAX_ITEMS:]

def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()

def log_trade(order: dict = None, result: str = None, error: str = None, raw_text: str = None):
    """
    记录一次交易事件。
    - order:    解析后的订单 dict（确认执行时传入）
    - result:   执行结果文本（成功时）
    - error:    错误信息（失败时）
    - raw_text: 原始指令文本（直接动作/无解析订单时传入）
    """
    entries = _load()
    now = time.time()
    entry = {
        "ts": int(now),
        "dt": datetime.fromtimestamp(now).strftime("%m-%d %H:%M:%S"),
        "ok": error is None,
    }

    if order:
        entry["action"]      = order.get("action", "?")
        entry["symbol"]      = order.get("symbol", "")
        entry["side"]        = order.get("side", "")
        entry["qty"]         = order.get("qty")
        entry["usdt"]        = order.get("usdt")
        entry["leverage"]    = order.get("leverage")
        entry["price_type"]  = order.get("price_type", "")
        entry["margin_mode"] = order.get("margin_mode", "cross")
        entry["sl"]          = order.get("sl")
        entry["tp"]          = order.get("tp")
        entry["dark_order"]  = order.get("dark_order", False)

    if raw_text:
        entry["raw"] = raw_text[:100]

    if result:
        entry["result"] = _strip_html(result)[:200]

    if error:
        entry["error"] = error[:200]

    entries.append(entry)
    entries = _rotate(entries)
    _save(entries)


def get_recent(limit: int = 20) -> list:
    """返回最近 limit 条，最新在前"""
    entries = _load()
    return list(reversed(entries))[:limit]


def format_for_tg(entries: list) -> str:
    """格式化成 Telegram HTML 消息"""
    if not entries:
        return "📭 暂无交易记录"
    lines = []
    for e in entries:
        icon   = "✅" if e.get("ok") else "❌"
        dt     = e.get("dt", "")
        action = e.get("action") or ""
        symbol = e.get("symbol") or ""
        raw    = e.get("raw") or ""
        # 显示名
        label  = f"{action} {symbol}".strip() if action else raw
        # 结果/错误
        body   = e.get("result") or e.get("error") or ""
        # 补充参数行
        params = []
        if e.get("leverage"):  params.append(f"{e['leverage']}x")
        if e.get("usdt"):      params.append(f"{e['usdt']}U")
        if e.get("qty"):       params.append(f"qty={e['qty']}")
        if e.get("sl"):        params.append(f"sl={e['sl']}")
        if e.get("tp"):        params.append(f"tp={e['tp']}")
        param_str = "  " + " ".join(params) if params else ""
        lines.append(
            f"{icon} <b>{dt}</b>  {label}\n"
            + (param_str + "\n" if param_str else "")
            + (f"  {body[:100]}" if body else "")
        )
    return "\n\n".join(lines)
