"""
rolling.py — 滚仓 v2.0
规则：浮盈 ≥50% 时，用盈利的70%加仓，峰值取 max(原峰值, 当前价)

API：
  execute_roll(symbol)  → 玄玄手动触发
  format_status()       → 查看滚仓历史
"""
import os
import json
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

ROLL_FILE  = Path(__file__).parent.parent / "data" / "rolling_state.json"
TRIGGER_PCT = float(os.getenv("ROLL_TRIGGER_PCT",  50))   # 浮盈触发阈值%
PROFIT_RATIO = float(os.getenv("ROLL_PROFIT_RATIO", 0.7)) # 盈利中用于加仓的比例
CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "509640925")


# ── 持久化 ────────────────────────────────────────────────

def _load() -> list:
    try:
        return json.loads(ROLL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(records: list):
    ROLL_FILE.parent.mkdir(parents=True, exist_ok=True)
    ROLL_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 核心执行 ──────────────────────────────────────────────

def execute_roll(symbol: str) -> str:
    """
    手动触发滚仓。
    检查浮盈是否达到50%，是则用盈利×70%加仓，并更新移动止盈峰值。
    """
    from trader.exchange import get_positions, get_mark_price, get_client, fix_qty, get_balance
    from trader.order import execute

    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    # ── 查持仓 ──
    positions = get_positions(symbol)
    if not positions:
        return f"❌ {symbol.replace('USDT','')} 当前无持仓"

    pos         = positions[0]
    amt         = float(pos["positionAmt"])
    side        = "long" if amt > 0 else "short"
    entry_price = float(pos["entryPrice"])
    cur_price   = get_mark_price(symbol)

    # ── 计算浮盈 ──
    if side == "long":
        float_pnl_pct = (cur_price - entry_price) / entry_price * 100
        float_pnl_usdt = (cur_price - entry_price) * abs(amt)
    else:
        float_pnl_pct = (entry_price - cur_price) / entry_price * 100
        float_pnl_usdt = (entry_price - cur_price) * abs(amt)

    coin = symbol.replace("USDT", "")

    if float_pnl_pct < TRIGGER_PCT:
        return (
            f"❌ {coin} 浮盈 {float_pnl_pct:.1f}%，未达到 {TRIGGER_PCT}%\n"
            f"还差 {TRIGGER_PCT - float_pnl_pct:.1f}% 才能滚仓"
        )

    # ── 计算加仓金额 ──
    add_usdt = round(float_pnl_usdt * PROFIT_RATIO, 2)

    # 检查可用余额
    bal = get_balance()
    if bal["available"] < add_usdt:
        return (
            f"⚠️ {coin} 可用余额不足\n"
            f"需要: {add_usdt}U  可用: {bal['available']:.2f}U"
        )

    # ── 推算加仓数量 ──
    # 获取当前杠杆（从持仓信息）
    leverage = int(float(pos.get("leverage", 10)))
    qty_usdt = add_usdt * leverage
    add_qty  = fix_qty(symbol, qty_usdt / cur_price)

    if float(add_qty) <= 0:
        return f"❌ {coin} 加仓数量太小，无法执行"

    # ── 执行加仓（暗单）──
    action = "open_long" if side == "long" else "open_short"
    result = execute({
        "action":      action,
        "symbol":      symbol,
        "usdt":        add_usdt,
        "leverage":    leverage,
        "margin_mode": pos.get("marginType", "cross").lower().replace("crossed", "cross"),
        "dark_order":  True,
    })

    # ── 更新移动止盈峰值（取最大值，不往低压）──
    _update_trailing_peak(symbol, cur_price)

    # ── 记录滚仓历史 ──
    records = _load()
    records.insert(0, {
        "symbol":        symbol,
        "side":          side,
        "entry_price":   entry_price,
        "roll_price":    cur_price,
        "float_pnl_pct": round(float_pnl_pct, 1),
        "add_usdt":      add_usdt,
        "rolled_at":     int(time.time()),
    })
    _save(records[:50])  # 保留最近50条

    direction = "多" if side == "long" else "空"
    return (
        f"✅ {coin} 滚仓执行\n"
        f"方向: {direction}  浮盈: +{float_pnl_pct:.1f}%\n"
        f"加仓: {add_usdt}U（盈利{float_pnl_usdt:.1f}U × {int(PROFIT_RATIO*100)}%）\n"
        f"移动止盈峰值已更新\n"
        f"{result}"
    )


def _update_trailing_peak(symbol: str, cur_price: float):
    """
    滚仓后更新移动止盈峰值：取 max(原峰值, 当前价)，不往低压。
    """
    try:
        from trader.trailing import STATE_FILE as TRAILING_STATE, _load as t_load, _save as t_save
        state = t_load()
        if symbol not in state:
            return
        entry = state[symbol]
        if not entry.get("activated"):
            return
        side = entry["side"]
        old_peak = entry["peak_price"]
        if side == "long":
            new_peak = max(old_peak, cur_price)
        else:
            new_peak = min(old_peak, cur_price)   # 空单：峰值是最低价
        if new_peak != old_peak:
            entry["peak_price"] = new_peak
            state[symbol] = entry
            t_save(state)
    except Exception:
        pass   # 没有激活的移动止盈，静默跳过


def format_status() -> str:
    """格式化最近滚仓记录"""
    records = _load()
    if not records:
        return "暂无滚仓记录"

    lines = ["📋 最近滚仓记录：\n"]
    for r in records[:5]:
        coin = r["symbol"].replace("USDT", "")
        direction = "多" if r["side"] == "long" else "空"
        t = time.strftime("%m-%d %H:%M", time.localtime(r["rolled_at"]))
        lines.append(
            f"{coin} {direction}  +{r['float_pnl_pct']}%时滚  加{r['add_usdt']}U  {t}"
        )
    return "\n".join(lines)
