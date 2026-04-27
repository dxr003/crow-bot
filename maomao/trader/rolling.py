"""
rolling.py — 滚仓 v2.1（多账户）
规则：浮盈 ≥50% 时，用盈利的70%加仓，峰值取 max(原峰值, 当前价)

多账户（2026-04-19）：
  - execute_roll(symbol, account="币安1") 接 account 参数，默认 币安1
  - 记录 schema 增加 "account" 字段
  - _update_trailing_peak 用 `symbol@account` 键，匹配 trailing v4.1

API：
  execute_roll(symbol, account="币安1")  → 玄玄手动触发
  format_status()                         → 查看滚仓历史
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

DEFAULT_ACCOUNT = "币安1"
ROLE = "大猫"  # role 用于 multi.executor 权限校验（大猫对所有账户有权）


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

def execute_roll(symbol: str, account: str = DEFAULT_ACCOUNT) -> str:
    """
    手动触发滚仓。
    检查浮盈是否达到50%，是则用盈利×70%加仓，并更新移动止盈峰值。
    """
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    # 解析账户别名
    try:
        from trader.multi.registry import _resolve_name
        account = _resolve_name(account)
    except Exception:
        pass

    coin = symbol.replace("USDT", "")

    # ── 查持仓 ──
    if account == DEFAULT_ACCOUNT:
        from trader.exchange import get_positions, get_mark_price, fix_qty, get_balance
        positions = get_positions(symbol)
        if not positions:
            return f"❌ {coin} 在 {account} 当前无持仓"
        cur_price = get_mark_price(symbol)
        bal = get_balance()
        avail = bal["available"]
    else:
        from trader.multi.registry import get_futures_client
        from trader.multi.executor import get_futures_only
        c = get_futures_client(account)
        positions = [p for p in c.get_position_risk(symbol=symbol)
                     if float(p.get("positionAmt", 0)) != 0]
        if not positions:
            return f"❌ {coin} 在 {account} 当前无持仓"
        cur_price = _mark_price_public(symbol)
        bal = get_futures_only(ROLE, account)
        avail = bal["available"]

    pos         = positions[0]
    amt         = float(pos["positionAmt"])
    side        = "long" if amt > 0 else "short"
    entry_price = float(pos["entryPrice"])

    # ── 计算浮盈 ──
    if side == "long":
        float_pnl_pct = (cur_price - entry_price) / entry_price * 100
        float_pnl_usdt = (cur_price - entry_price) * abs(amt)
    else:
        float_pnl_pct = (entry_price - cur_price) / entry_price * 100
        float_pnl_usdt = (entry_price - cur_price) * abs(amt)

    if float_pnl_pct < TRIGGER_PCT:
        return (
            f"❌ {coin}@{account} 浮盈 {float_pnl_pct:.1f}%，未达到 {TRIGGER_PCT}%\n"
            f"还差 {TRIGGER_PCT - float_pnl_pct:.1f}% 才能滚仓"
        )

    # ── 计算加仓金额 ──
    add_usdt = round(float_pnl_usdt * PROFIT_RATIO, 2)

    if avail < add_usdt:
        return (
            f"⚠️ {coin}@{account} 可用余额不足\n"
            f"需要: {add_usdt}U  可用: {avail:.2f}U"
        )

    leverage = int(float(pos.get("leverage", 10)))

    # ── 执行加仓 ──
    if account == DEFAULT_ACCOUNT:
        # 币安1：走 trader.order 保留 dark_order
        from trader.order import execute
        action = "open_long" if side == "long" else "open_short"
        result = execute({
            "action":      action,
            "symbol":      symbol,
            "usdt":        add_usdt,
            "leverage":    leverage,
            "margin_mode": pos.get("marginType", "cross").lower().replace("crossed", "cross"),
            "dark_order":  True,
        })
    else:
        # 其他账户：走 multi.executor
        from trader.multi.executor import open_market
        bn_side = "BUY" if side == "long" else "SELL"
        margin_type = pos.get("marginType", "cross").upper()
        if "CROSS" in margin_type:
            margin_type = "CROSSED"
        else:
            margin_type = "ISOLATED"
        r = open_market(ROLE, account, symbol=symbol, side=bn_side,
                        margin=add_usdt, leverage=leverage, margin_type=margin_type)
        if r.get("error"):
            result = f"❌ 加仓失败: {r['error']}"
        else:
            result = f"✅ 加仓 {r['qty']} @ ~{r['price']} orderId={r.get('orderId','?')}"

    # ── 更新移动止盈峰值 ──
    _update_trailing_peak(symbol, account, cur_price)

    # ── 记录滚仓历史 ──
    records = _load()
    records.insert(0, {
        "symbol":        symbol,
        "account":       account,
        "side":          side,
        "entry_price":   entry_price,
        "roll_price":    cur_price,
        "float_pnl_pct": round(float_pnl_pct, 1),
        "add_usdt":      add_usdt,
        "rolled_at":     int(time.time()),
    })
    _save(records[:50])

    direction = "多" if side == "long" else "空"
    acct_tag = "" if account == DEFAULT_ACCOUNT else f"（{account}）"
    return (
        f"✅ {coin}{acct_tag} 滚仓执行\n"
        f"方向: {direction}  浮盈: +{float_pnl_pct:.1f}%\n"
        f"加仓: {add_usdt}U（盈利{float_pnl_usdt:.1f}U × {int(PROFIT_RATIO*100)}%）\n"
        f"移动止盈峰值已更新\n"
        f"{result}"
    )


def _mark_price_public(symbol: str) -> float:
    resp = requests.get(
        "https://fapi.binance.com/fapi/v1/premiumIndex",
        params={"symbol": symbol}, timeout=5,
    )
    return float(resp.json()["markPrice"])


def _update_trailing_peak(symbol: str, account: str, cur_price: float):
    """滚仓后推高/压低移动止盈峰值，若无激活则静默跳过。"""
    try:
        from trader.trailing import update_peak
        update_peak(symbol, account, cur_price)
    except Exception:
        pass


def format_status() -> str:
    """格式化最近滚仓记录"""
    records = _load()
    if not records:
        return "暂无滚仓记录"

    lines = ["📋 最近滚仓记录：\n"]
    for r in records[:5]:
        coin = r["symbol"].replace("USDT", "")
        direction = "多" if r["side"] == "long" else "空"
        acct = r.get("account", DEFAULT_ACCOUNT)
        acct_tag = "" if acct == DEFAULT_ACCOUNT else f"@{acct}"
        t = time.strftime("%m-%d %H:%M", time.localtime(r["rolled_at"]))
        lines.append(
            f"{coin}{acct_tag} {direction}  +{r['float_pnl_pct']}%时滚  加{r['add_usdt']}U  {t}"
        )
    return "\n".join(lines)
