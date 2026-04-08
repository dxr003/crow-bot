"""
trailing.py — 移动止盈 v3.1
规则：浮盈达到激活阈值后追踪峰值，回撤25%（+3%容错防抖）触发全平

API：
  activate(symbol, threshold=40)  → 开启追踪
  deactivate(symbol)              → 取消追踪
  check_all()                     → cron调用，检查并触发平仓
  format_status()                 → 玄玄展示当前追踪列表
"""
import os
import json
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

STATE_FILE = Path(__file__).parent.parent / "data" / "trailing_state.json"

PULLBACK_TRIGGER = float(os.getenv("TRAILING_PULLBACK",   25))   # 回撤触发%
TOLERANCE        = float(os.getenv("TRAILING_TOLERANCE",   3))   # 容错防抖% (加在触发阈值上)
DEFAULT_ACTIVATE = float(os.getenv("TRAILING_ACTIVATION", 40))   # 默认激活阈值%

CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "509640925")


# ── 状态持久化 ────────────────────────────────────────────

def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 核心操作 ──────────────────────────────────────────────

def activate(symbol: str, threshold: float = None) -> str:
    """
    为当前持仓开启移动止盈追踪。
    threshold: 激活浮盈阈值%，None 用默认值（40%）
    """
    from trader.exchange import get_positions, get_mark_price

    if threshold is None:
        threshold = DEFAULT_ACTIVATE

    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    positions = get_positions(symbol)
    if not positions:
        return f"❌ {symbol} 当前无持仓，无法开启移动止盈"

    pos = positions[0]
    amt         = float(pos["positionAmt"])
    side        = "long" if amt > 0 else "short"
    entry_price = float(pos["entryPrice"])
    cur_price   = get_mark_price(symbol)

    float_pnl = _calc_pnl(side, entry_price, cur_price)
    already_active = float_pnl >= threshold

    state = _load()
    state[symbol] = {
        "side":               side,
        "entry_price":        entry_price,
        "activation_threshold": threshold,
        "activated":          already_active,
        "peak_price":         cur_price if already_active else entry_price,
        "started_at":         int(time.time()),
        "activated_at":       int(time.time()) if already_active else None,
    }
    _save(state)

    coin = symbol.replace("USDT", "")
    if already_active:
        trigger_price = _trigger_price(side, cur_price)
        return (
            f"✅ {coin} 移动止盈已激活\n"
            f"方向: {'多' if side == 'long' else '空'}  入场: {entry_price}\n"
            f"当前浮盈: +{float_pnl:.1f}%  峰值: {cur_price}\n"
            f"触发价: {trigger_price:.4f}（峰值回撤 {PULLBACK_TRIGGER}%）"
        )
    else:
        return (
            f"✅ {coin} 移动止盈已设置\n"
            f"方向: {'多' if side == 'long' else '空'}  入场: {entry_price}\n"
            f"当前浮盈: {float_pnl:.1f}%，等待达到 +{threshold}% 后开始追踪"
        )


def deactivate(symbol: str) -> str:
    """取消某持仓的移动止盈追踪"""
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    state = _load()
    if symbol not in state:
        return f"❌ {symbol.replace('USDT','')} 未在追踪中"

    del state[symbol]
    _save(state)
    return f"✅ {symbol.replace('USDT','')} 移动止盈已取消"


def format_status() -> str:
    """格式化当前追踪列表，供玄玄展示"""
    from trader.exchange import get_mark_price

    state = _load()
    if not state:
        return "当前无移动止盈追踪"

    lines = ["📊 移动止盈追踪中：\n"]
    for symbol, entry in state.items():
        coin        = symbol.replace("USDT", "")
        side        = entry["side"]
        entry_price = entry["entry_price"]
        threshold   = entry["activation_threshold"]
        activated   = entry["activated"]
        peak        = entry["peak_price"]

        try:
            cur_price = get_mark_price(symbol)
            float_pnl = _calc_pnl(side, entry_price, cur_price)
            pnl_str   = f"{float_pnl:+.1f}%"
        except Exception:
            cur_price = None
            pnl_str   = "获取中"

        direction = "多" if side == "long" else "空"

        if activated:
            tp = _trigger_price(side, peak)
            lines.append(
                f"{coin} {direction}  追踪中🟢\n"
                f"  浮盈: {pnl_str}  峰值: {peak:.4f}\n"
                f"  触发价: {tp:.4f}（回撤{PULLBACK_TRIGGER}%）\n"
            )
        else:
            lines.append(
                f"{coin} {direction}  等待激活⏳\n"
                f"  浮盈: {pnl_str}  激活阈值: +{threshold}%\n"
            )

    return "\n".join(lines)


# ── cron 入口 ─────────────────────────────────────────────

def check_all() -> list:
    """
    检查所有追踪持仓，条件满足则触发平仓。
    返回触发列表，供 cron 脚本记录日志。
    """
    from trader.exchange import get_positions, get_mark_price
    from trader.order import execute

    state = _load()
    if not state:
        return []

    triggered = []
    changed   = False

    for symbol, entry in list(state.items()):
        try:
            positions = get_positions(symbol)
            if not positions:
                # 持仓消失（被手动平掉），清理追踪
                del state[symbol]
                changed = True
                _notify(f"ℹ️ {symbol.replace('USDT','')} 持仓已消失，移动止盈追踪自动清除")
                continue

            cur_price   = get_mark_price(symbol)
            side        = entry["side"]
            entry_price = entry["entry_price"]
            threshold   = entry["activation_threshold"]
            peak        = entry["peak_price"]
            float_pnl   = _calc_pnl(side, entry_price, cur_price)

            # ── 未激活：等待浮盈达阈值 ──
            if not entry["activated"]:
                if float_pnl >= threshold:
                    entry["activated"]    = True
                    entry["activated_at"] = int(time.time())
                    entry["peak_price"]   = cur_price
                    state[symbol] = entry
                    changed = True
                    coin = symbol.replace("USDT", "")
                    tp   = _trigger_price(side, cur_price)
                    _notify(
                        f"🟢 {coin} 移动止盈激活！\n"
                        f"浮盈 +{float_pnl:.1f}% 已达 +{threshold}%\n"
                        f"峰值: {cur_price}  触发价: {tp:.4f}"
                    )
                continue

            # ── 已激活：更新峰值 ──
            if side == "long" and cur_price > peak:
                entry["peak_price"] = cur_price
                state[symbol] = entry
                changed = True
                peak = cur_price
            elif side == "short" and cur_price < peak:
                entry["peak_price"] = cur_price
                state[symbol] = entry
                changed = True
                peak = cur_price

            # ── 计算回撤，判断是否触发 ──
            drawdown = _drawdown(side, peak, cur_price)

            if drawdown >= PULLBACK_TRIGGER + TOLERANCE:
                # 先撤掉该币种所有挂单（止盈/止损），避免孤单
                from trader.exchange import cancel_all_orders
                cancel_all_orders(symbol)

                action = "close_long" if side == "long" else "close_short"
                result = execute({"action": action, "symbol": symbol, "dark_order": True})

                del state[symbol]
                changed = True

                coin = symbol.replace("USDT", "")
                _notify(
                    f"🔔 移动止盈触发 — {coin}\n"
                    f"方向: {'多' if side == 'long' else '空'}\n"
                    f"入场: {entry_price}  峰值: {peak:.4f}  当前: {cur_price:.4f}\n"
                    f"峰值回撤: -{drawdown:.1f}%  浮盈: {float_pnl:+.1f}%\n"
                    f"{result}"
                )
                triggered.append({
                    "symbol":   symbol,
                    "side":     side,
                    "pnl_pct":  round(float_pnl, 1),
                    "drawdown": round(drawdown, 1),
                })

        except Exception as e:
            _notify(f"⚠️ {symbol.replace('USDT','')} 移动止盈检查出错: {e}")

    if changed:
        _save(state)

    return triggered


# ── 工具函数 ──────────────────────────────────────────────

def _calc_pnl(side: str, entry: float, cur: float) -> float:
    if side == "long":
        return (cur - entry) / entry * 100
    return (entry - cur) / entry * 100


def _drawdown(side: str, peak: float, cur: float) -> float:
    if side == "long":
        return (peak - cur) / peak * 100
    return (cur - peak) / peak * 100


def _trigger_price(side: str, peak: float) -> float:
    if side == "long":
        return peak * (1 - PULLBACK_TRIGGER / 100)
    return peak * (1 + PULLBACK_TRIGGER / 100)


def _notify(text: str):
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception:
        pass
