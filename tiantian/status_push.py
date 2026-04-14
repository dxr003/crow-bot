#!/usr/bin/env python3
"""
status_push.py — 币安2持仓快照，每小时55分推送
推送对象：乌鸦 + 震天响
cron: 55 * * * * cd /root/tiantian && /root/tiantian/venv/bin/python status_push.py >> logs/status.log 2>&1
"""
import os
import sys
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WUYA_ID = "509640925"
ZHENTIANXIANG_ID = "5700670381"


def _send(chat_id: str, text: str):
    if not BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def main():
    from trader.exchange import get_positions, get_mark_price, get_balance

    positions = get_positions()
    lines = [f"📋 <b>币安2 持仓快照 {time.strftime('%H:%M')}</b>\n"]

    if not positions:
        lines.append("当前无持仓")
    else:
        for p in positions:
            symbol = p["symbol"]
            amt = float(p["positionAmt"])
            side = "多" if amt > 0 else "空"
            entry = float(p["entryPrice"])
            upnl = float(p["unRealizedProfit"])
            lev = p.get("leverage", "?")
            try:
                cur = get_mark_price(symbol)
                price_pct = (cur - entry) / entry * 100 if amt > 0 else (entry - cur) / entry * 100
                notional = abs(float(p.get("notional", 0)))
                init_margin = float(p.get("initialMargin", 0))
                if init_margin > 0:
                    lev_num = max(1, round(notional / init_margin))
                else:
                    lev_num = float(lev) if str(lev).replace('.', '').isdigit() else 1
                margin_pct = price_pct * lev_num
                lines.append(
                    f"{symbol.replace('USDT', '')} {side} {lev_num}x  "
                    f"入场:{entry:.4f}  浮盈:{upnl:+.2f}U ({margin_pct:+.1f}%)"
                )
            except Exception:
                lines.append(f"{symbol.replace('USDT', '')} {side}  入场:{entry:.4f}  浮盈:{upnl:+.2f}U")

    try:
        bal = get_balance()
        lines.append(f"\n💰 余额:{bal['total']:.2f}U  可用:{bal['available']:.2f}U  浮盈:{bal['upnl']:+.2f}U")
    except Exception:
        pass

    text = "\n".join(lines)
    _send(WUYA_ID, text)
    _send(ZHENTIANXIANG_ID, text)
    print(f"[{time.strftime('%H:%M:%S')}] 币安2快照已推送")


if __name__ == "__main__":
    main()
