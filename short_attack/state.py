"""
state.py — 状态机，管理监控/持仓/退出全生命周期
只负责状态判断和持久化，不推送，不调交易API
"""
import os
import json
import time
from pathlib import Path

STATE_FILE = Path("/root/short_attack/data/state.json")

# 配置参数（从env读取，有默认值）
MONITOR_THRESHOLD  = float(os.getenv("MONITOR_THRESHOLD", 80))
SIGNAL_RISE        = float(os.getenv("SIGNAL_RISE", 150))
SIGNAL_PULLBACK    = float(os.getenv("SIGNAL_PULLBACK", 20))
EXIT_THRESHOLD     = float(os.getenv("EXIT_THRESHOLD", 60))
SUCCESS_DROP       = float(os.getenv("SUCCESS_DROP", 50))
LIQ_MULTIPLIER     = float(os.getenv("LIQ_MULTIPLIER", 1.20))
REENTRY_COOLDOWN_H = float(os.getenv("REENTRY_COOLDOWN_H", 24))
MONITOR_EXPIRE_H   = float(os.getenv("MONITOR_EXPIRE_H", 72))
CARD_INTERVAL_MIN  = float(os.getenv("CARD_INTERVAL_MIN", 60))


def load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _empty_state()

def save(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def _empty_state() -> dict:
    return {
        "monitoring":   {},   # symbol → 监控数据
        "signals":      {},   # symbol → 持仓信号数据
        "exits":        [],   # 最近退出记录
        "cooldowns":    {},   # symbol → 退出时间戳（冷却用）
        "stats":        {"success": 0, "failed": 0},
        "last_card_at": 0,
    }


# ── 主要处理函数（每次扫描调用）──────────────────────────

def process_tick(tickers: list[dict]) -> dict:
    """
    处理一次扫描结果，更新状态，返回本次触发的事件。
    返回格式：
    {
        "new_monitors":  [{"symbol":..., "price":..., "change_pct":..., "volume_usdt":...}],
        "new_signals":   [{"symbol":..., "position_price":..., "liq_price":...}],
        "new_exits":     [{"symbol":..., "reason":..., "max_pct":..., "exit_pct":...}],
        "send_card":     bool,  # 是否到了推定时卡片的时间
    }
    """
    state  = load()
    now    = time.time()
    events = {"new_monitors": [], "new_signals": [], "new_exits": [], "send_card": False}

    ticker_map = {t["symbol"]: t for t in tickers}

    # ── 1. 检查现有监控中的币 ──────────────────────────────
    for symbol, mon in list(state["monitoring"].items()):
        ticker = ticker_map.get(symbol)
        cur_price = ticker["price"] if ticker else None

        # 获取不到价格，跳过本轮
        if cur_price is None or cur_price <= 0:
            continue

        entry_price = mon["price_at_entry"]

        # 更新最高价
        if cur_price > mon.get("max_price", entry_price):
            mon["max_price"] = cur_price
            state["monitoring"][symbol] = mon

        max_price = mon["max_price"]

        # 判断是否触发持仓信号
        rise_from_entry = (max_price - entry_price) / entry_price * 100
        pullback_from_max = (max_price - cur_price) / max_price * 100

        if rise_from_entry >= SIGNAL_RISE and pullback_from_max >= SIGNAL_PULLBACK:
            # 升级为持仓信号
            liq_price = round(cur_price * LIQ_MULTIPLIER, 8)
            signal_data = {
                "price_at_entry":  entry_price,
                "position_price":  cur_price,
                "liq_price":       liq_price,
                "max_price":       max_price,
                "entry_gain_pct":  mon.get("entry_gain_pct", 0),
                "rise_from_entry": round(rise_from_entry, 1),
                "pullback_pct":    round(pullback_from_max, 1),
                "triggered_at":    now,
                "volume_usdt":     mon.get("volume_usdt", 0),
                "funding_rate":    mon.get("funding_rate", 0),
                "oi_change_pct":   mon.get("oi_change_pct", 0),
            }
            state["signals"][symbol] = signal_data
            del state["monitoring"][symbol]
            events["new_signals"].append({"symbol": symbol, **signal_data})
            continue

        # 判断自然退出：跌回发现价的(1 - EXIT_THRESHOLD/100)以下
        exit_price_line = entry_price * (1 - EXIT_THRESHOLD / 100)
        elapsed_h = (now - mon["started_at"]) / 3600

        exit_reason = None
        if cur_price <= exit_price_line:
            exit_reason = "涨幅回落"
        elif elapsed_h >= MONITOR_EXPIRE_H:
            exit_reason = "监控超时"

        if exit_reason:
            exit_pct = round((cur_price - entry_price) / entry_price * 100, 1)
            max_pct  = round((max_price - entry_price) / entry_price * 100, 1)
            _add_exit(state, symbol, max_pct, exit_pct, exit_reason, now)
            del state["monitoring"][symbol]
            events["new_exits"].append({
                "symbol": symbol, "reason": exit_reason,
                "max_pct": max_pct, "exit_pct": exit_pct,
            })

    # ── 2. 检查持仓信号中的币 ──────────────────────────────
    for symbol, sig in list(state["signals"].items()):
        ticker = ticker_map.get(symbol)
        cur_price = ticker["price"] if ticker else None

        if cur_price is None or cur_price <= 0:
            continue

        position_price = sig["position_price"]

        # 更新最高价（持仓后可能继续涨）
        if cur_price > sig.get("max_price", position_price):
            sig["max_price"] = cur_price
            state["signals"][symbol] = sig

        # 击中：从持仓价跌超 SUCCESS_DROP%
        drop_pct = (position_price - cur_price) / position_price * 100
        if drop_pct >= SUCCESS_DROP:
            max_pct  = round((sig["max_price"] - sig["price_at_entry"]) / sig["price_at_entry"] * 100, 1)
            exit_pct = round((cur_price - sig["price_at_entry"]) / sig["price_at_entry"] * 100, 1)
            _add_exit(state, symbol, max_pct, exit_pct, "阻击成功", now)
            del state["signals"][symbol]
            state["stats"]["success"] += 1
            events["new_exits"].append({
                "symbol": symbol, "reason": "阻击成功",
                "max_pct": max_pct, "exit_pct": exit_pct,
            })
            continue

        # 失败：超过强平价
        if cur_price >= sig["liq_price"]:
            max_pct  = round((sig["max_price"] - sig["price_at_entry"]) / sig["price_at_entry"] * 100, 1)
            exit_pct = round((cur_price - sig["price_at_entry"]) / sig["price_at_entry"] * 100, 1)
            _add_exit(state, symbol, max_pct, exit_pct, "阻击失败", now)
            del state["signals"][symbol]
            state["stats"]["failed"] += 1
            events["new_exits"].append({
                "symbol": symbol, "reason": "阻击失败",
                "max_pct": max_pct, "exit_pct": exit_pct,
            })

    # ── 3. 扫描新进入监控的币 ──────────────────────────────
    for t in tickers:
        symbol    = t["symbol"]
        cur_price = t["price"]
        change    = t["change_pct"]

        # 已在监控或信号中，跳过
        if symbol in state["monitoring"] or symbol in state["signals"]:
            continue

        # 冷却期检查
        cooldown_ts = state["cooldowns"].get(symbol, 0)
        if now - cooldown_ts < REENTRY_COOLDOWN_H * 3600:
            continue

        # 涨幅达到阈值
        if change >= MONITOR_THRESHOLD:
            state["monitoring"][symbol] = {
                "price_at_entry": cur_price,
                "max_price":      cur_price,
                "entry_gain_pct": round(change, 1),
                "started_at":     now,
                "volume_usdt":    t.get("volume_usdt", 0),
                "funding_rate":   0.0,   # 后续由 main.py 补充
                "oi_change_pct":  0.0,
            }
            events["new_monitors"].append({
                "symbol":      symbol,
                "price":       cur_price,
                "change_pct":  round(change, 1),
                "volume_usdt": t.get("volume_usdt", 0),
            })

    # ── 4. 判断是否推定时卡片 ──────────────────────────────
    if now - state["last_card_at"] >= CARD_INTERVAL_MIN * 60:
        events["send_card"] = True
        state["last_card_at"] = now

    save(state)
    return events


def _add_exit(state: dict, symbol: str, max_pct: float, exit_pct: float, reason: str, now: float):
    """记录退出，保留最近20条，设置冷却"""
    state["exits"].insert(0, {
        "symbol":    symbol,
        "max_pct":   max_pct,
        "exit_pct":  exit_pct,
        "reason":    reason,
        "exited_at": int(now),
    })
    state["exits"] = state["exits"][:20]
    state["cooldowns"][symbol] = now


def get_snapshot() -> dict:
    """返回当前完整状态快照，供 notifier 使用"""
    return load()
