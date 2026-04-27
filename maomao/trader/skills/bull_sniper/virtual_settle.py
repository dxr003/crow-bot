"""virtual_settle — 信号虚拟结算器（alert 模式下也能出胜率）

用 sniper_bull_limit profile 的参数给每条 state.signals 空推判定：
  - 激活:  峰值涨幅 ≥ ACTIVATE_PCT  (开始跟踪移动止盈)
  - 止盈:  已激活 + 从峰值回撤 ≥ RETRACE_PCT → success
  - 止损:  当前涨幅 ≤ STOP_LOSS_PCT        → failed
  - 超时:  距首次触发 ≥ TIMEOUT_HOURS       → expired

触发任一条件就从 state.signals 移除 → append 到 state.signal_history
标记 is_virtual=True, 卡片会自动渲染到"📊 虚拟成绩（未真实开仓）"分组。
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Optional

logger = logging.getLogger("bull_sniper.virtual_settle")

# 参数对齐 trader/trailing_layered.py 的 sniper_bull_limit profile
ACTIVATE_PCT = 25.0
RETRACE_PCT = 20.0
STOP_LOSS_PCT = -10.0
TIMEOUT_HOURS = 24


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _parse_signal_time(ts: str) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def check_and_settle(state: dict, price_map: dict) -> list:
    """扫 signals，命中结算条件迁移到 signal_history。返回本轮结算的记录列表。"""
    signals = state.get("signals", [])
    if not signals:
        return []

    history = state.setdefault("signal_history", [])
    positions = state.get("positions", {}) or {}
    settled: list[dict] = []
    keep: list[dict] = []
    now = _dt.datetime.now()

    for sig in signals:
        symbol = sig.get("symbol", "")
        # 已进真实持仓 → 由 bull_trailing 真实结算，virtual 不碰
        if symbol in positions:
            keep.append(sig)
            continue
        entry = float(sig.get("entry_price", 0) or 0)
        cur = float(price_map.get(symbol, sig.get("cur_price", 0)) or 0)
        if entry <= 0 or cur <= 0:
            keep.append(sig)
            continue

        peak = max(float(sig.get("peak_price", entry) or entry), cur)
        peak_pct = (peak - entry) / entry * 100
        gain_pct = (cur - entry) / entry * 100
        sig["peak_price"] = peak
        sig["peak_pct"] = round(peak_pct, 2)
        sig["cur_price"] = cur
        sig["gain_pct"] = round(gain_pct, 2)

        if not sig.get("activated") and peak_pct >= ACTIVATE_PCT:
            sig["activated"] = True
            sig["activated_at"] = _now_iso()

        exit_reason = None
        status = None

        if sig.get("activated"):
            retrace = (peak - cur) / peak * 100 if peak > 0 else 0.0
            if retrace >= RETRACE_PCT:
                exit_reason = "tp_trail"
                status = "success" if cur > entry else "failed"

        if exit_reason is None and gain_pct <= STOP_LOSS_PCT:
            exit_reason = "stop_loss"
            status = "failed"

        if exit_reason is None:
            sig_time = _parse_signal_time(sig.get("time", ""))
            if sig_time and (now - sig_time).total_seconds() >= TIMEOUT_HOURS * 3600:
                exit_reason = "timeout"
                status = "expired"

        if exit_reason is None:
            keep.append(sig)
            continue

        record = {
            "symbol": symbol,
            "time": sig.get("time"),
            "entry_price": entry,
            "exit_price": cur,
            "exit_reason": exit_reason,
            "status": status,
            "pnl_pct": round(gain_pct, 2),
            "peak_pct": round(peak_pct, 2),
            "is_virtual": True,
            "settled_at": _now_iso(),
        }
        history.append(record)
        settled.append(record)
        logger.info(
            f"[virtual_settle] {symbol} {status} via {exit_reason} "
            f"entry={entry} exit={cur} pnl={gain_pct:+.1f}% peak={peak_pct:+.1f}%"
        )

    state["signals"] = keep
    return settled
