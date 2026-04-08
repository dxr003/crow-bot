#!/usr/bin/env python3
"""
status_push.py — 每小时55分推送执行中订单状态
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
TOKEN   = os.getenv("BOT_TOKEN", "")


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


def main():
    from trader.exchange import get_positions, get_mark_price
    from trader.trailing import format_status as trailing_status

    positions = get_positions()
    lines = [f"📋 <b>持仓快照 {time.strftime('%H:%M')}</b>\n"]

    if not positions:
        lines.append("当前无持仓")
    else:
        for p in positions:
            symbol = p["symbol"]
            amt    = float(p["positionAmt"])
            side   = "多" if amt > 0 else "空"
            entry  = float(p["entryPrice"])
            upnl   = float(p["unRealizedProfit"])
            lev    = p.get("leverage", "?")
            try:
                cur = get_mark_price(symbol)
                pct = (cur - entry) / entry * 100 if amt > 0 else (entry - cur) / entry * 100
                lines.append(
                    f"{symbol.replace('USDT','')} {side} {lev}x  "
                    f"入场:{entry:.4f}  浮盈:{upnl:+.2f}U ({pct:+.1f}%)"
                )
            except Exception:
                lines.append(f"{symbol.replace('USDT','')} {side}  入场:{entry:.4f}  浮盈:{upnl:+.2f}U")

    # 移动止盈状态
    trailing = trailing_status()
    if trailing != "当前无移动止盈追踪":
        lines.append(f"\n{trailing}")

    # 滚仓名单
    watch_file = Path("/root/short_attack/data/roll_watch.json")
    if watch_file.exists():
        watch = json.loads(watch_file.read_text())
        if watch:
            coins = [s.replace("USDT","") for s in watch]
            lines.append(f"\n🔄 滚仓监控中：{' '.join(coins)}")

    _send("\n".join(lines))
    print(f"[{time.strftime('%H:%M:%S')}] 状态已推送")


if __name__ == "__main__":
    main()
