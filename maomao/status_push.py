#!/usr/bin/env python3
"""
status_push.py — 每小时55分推送多账户持仓快照
cron: 55 * * * * cd /root/maomao && python3 status_push.py >> logs/status.log 2>&1
"""
import os
import sys
import json
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "509640925")
TOKEN   = os.getenv("PUSH_BOT_TOKEN", "")


def _send(text: str):
    if not TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def _fmt_price(price: float) -> str:
    if price >= 100:   return f"{price:.2f}"
    if price >= 1:     return f"{price:.3f}"
    if price >= 0.01:  return f"{price:.4f}"
    return f"{price:.6f}"


def _pnl_icon(pct: float) -> str:
    if pct >= 20: return "🟣"
    if pct >= 5:  return "🟢"
    if pct >= 0:  return "⚪"
    if pct >= -10: return "🟡"
    return "🔴"


def _format_position(p: dict, get_mark_fn) -> str:
    symbol = p["symbol"]
    base = symbol.replace("USDT", "")
    amt = float(p["positionAmt"])
    side = "多" if amt > 0 else "空"
    entry = float(p["entryPrice"])
    upnl = float(p["unRealizedProfit"])
    notional = abs(float(p.get("notional", 0)))
    init_margin = float(p.get("initialMargin", 0))
    liq = float(p.get("liquidationPrice", 0))

    if init_margin > 0:
        lev = max(1, round(notional / init_margin))
    else:
        lev = 1

    try:
        mark = get_mark_fn(symbol)
    except Exception:
        mark = entry

    if entry > 0:
        price_pct = (mark - entry) / entry * 100 if amt > 0 else (entry - mark) / entry * 100
    else:
        price_pct = 0
    roi = price_pct * lev
    icon = _pnl_icon(roi)

    margin_type = "逐仓" if liq > 0 else "全仓"
    liq_str = f"\n     强平 <code>{_fmt_price(liq)}</code>" if liq > 0 else ""

    return (
        f"\n  {icon} <b>{base}</b>  {side} {lev}x {margin_type}\n"
        f"     入场 <code>{_fmt_price(entry)}</code>  →  现价 <code>{_fmt_price(mark)}</code>\n"
        f"     浮盈 <code>{upnl:+.2f}U</code>  收益率 <b>{roi:+.1f}%</b>{liq_str}"
    )


def main():
    from trader.multi_account import get_all_positions, get_all_balances
    from trader.exchange import get_mark_price
    from trader.trailing import format_status as trailing_status

    positions = get_all_positions()
    balances = get_all_balances()

    lines = [
        f"📋 <b>持仓快照</b>  {time.strftime('%H:%M')}",
        "━━━━━━━━━━━━━━━",
    ]

    real_positions = [p for p in positions if "_error" not in p]
    error_accounts = [p for p in positions if "_error" in p]

    by_account = {}
    for p in real_positions:
        acct = p.get("_account", "未知")
        by_account.setdefault(acct, []).append(p)

    acct_names = [b.get("name") for b in balances if "error" not in b]
    for acct_name in acct_names:
        acct_pos = by_account.get(acct_name, [])
        lines.append(f"\n<b>【{acct_name}】</b>")
        if acct_pos:
            for p in acct_pos:
                lines.append(_format_position(p, get_mark_price))
        else:
            lines.append("  无持仓")

    for err in error_accounts:
        lines.append(f"\n⚠️ {err['_account']}：查询失败")

    lines.append("\n━━━━━━━━━━━━━━━")
    for bal in balances:
        name = bal.get("name", "?")
        if "error" in bal:
            lines.append(f"【{name}】查询失败")
            continue
        futures = bal.get("futures", 0)
        avail = bal.get("futures_avail", 0)
        upnl = bal.get("futures_upnl", 0)
        lines.append(
            f"<b>【{name}】</b> "
            f"合约 <code>{futures:.2f}U</code>  "
            f"可用 <code>{avail:.2f}U</code>  "
            f"浮盈 <code>{upnl:+.2f}U</code>"
        )
        _stable = {"USDT", "USDC", "FDUSD", "BUSD", "DAI", "TUSD"}
        spot_u = sum(v for a, v in bal.get("spot", {}).items() if a in _stable and v > 0.01)
        fund_u = sum(v for a, v in bal.get("funding", {}).items() if a in _stable and v > 0.01)
        if spot_u > 1:
            lines.append(f"  💰现货 <code>{spot_u:.2f}U</code>")
        if fund_u > 1:
            lines.append(f"  💰资金 <code>{fund_u:.2f}U</code>")

    _stable = {"USDT", "USDC", "FDUSD", "BUSD", "DAI", "TUSD"}
    ok_bals = [b for b in balances if "error" not in b]
    total_futures = sum(b.get("futures", 0) for b in ok_bals)
    total_spot = sum(
        sum(v for a, v in b.get("spot", {}).items() if a in _stable)
        for b in ok_bals
    )
    total_fund = sum(
        sum(v for a, v in b.get("funding", {}).items() if a in _stable)
        for b in ok_bals
    )
    total_upnl = sum(b.get("futures_upnl", 0) for b in ok_bals)
    total_all = total_futures + total_spot + total_fund
    if len(ok_bals) > 1:
        lines.append(f"📊 <b>合计</b>  {total_all:.2f}U  浮盈 {total_upnl:+.2f}U")

    trailing = trailing_status()
    if trailing != "当前无移动止盈追踪":
        lines.append(f"\n🎯 {trailing}")

    watch_file = Path("/root/short_attack/data/roll_watch.json")
    if watch_file.exists():
        try:
            watch = json.loads(watch_file.read_text())
            if watch:
                coins = [s.replace("USDT", "") for s in watch]
                lines.append(f"🔄 滚仓监控：{' '.join(coins)}")
        except Exception:
            pass

    _send("\n".join(lines))
    print(f"[{time.strftime('%H:%M:%S')}] 多账户状态已推送")


if __name__ == "__main__":
    main()
