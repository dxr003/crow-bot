#!/usr/bin/env python3
"""
合约交易日报 — 每日00:01发送至贝贝bot→乌鸦私信
拉取币安1/币安2的当日已实现盈亏，按账户分组展示
"""
import hmac
import hashlib
import time
import os
import requests
from datetime import datetime, timedelta
from urllib.parse import urlencode
from pathlib import Path
from dotenv import dotenv_values

FAPI_BASE = "https://fapi.binance.com"

ACCOUNTS = [
    {
        "name": "币安1",
        "env_file": "/root/maomao/.env",
        "key_name": "BINANCE_API_KEY",
        "secret_name": "BINANCE_SECRET_KEY",
    },
    {
        "name": "币安2",
        "env_file": "/root/.qixing_env",
        "key_name": "BINANCE2_API_KEY",
        "secret_name": "BINANCE2_API_SECRET",
    },
]

# 贝贝bot → 乌鸦私信
maomao_env = dotenv_values("/root/maomao/.env")
BOT_TOKEN = maomao_env.get("PUSH_BOT_TOKEN", "")
CHAT_ID = maomao_env.get("ADMIN_ID", "509640925")


def _signed_get(key: str, secret: str, path: str, params: dict) -> list:
    params["timestamp"] = str(int(time.time() * 1000))
    qs = urlencode(params)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    resp = requests.get(
        f"{FAPI_BASE}{path}",
        params=qs + "&signature=" + sig,
        headers={"X-MBX-APIKEY": key},
        timeout=15,
    )
    if resp.status_code == 200:
        return resp.json()
    return []


def get_realized_pnl(acct: dict, start_ms: int, end_ms: int) -> dict:
    """拉取指定时间范围内的已实现盈亏，按symbol汇总"""
    vals = dotenv_values(acct["env_file"])
    key = vals.get(acct["key_name"], "")
    secret = vals.get(acct["secret_name"], "")
    if not key or not secret:
        return {}

    all_records = []
    # 分页拉取（每页最多1000条）
    params = {
        "incomeType": "REALIZED_PNL",
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": "1000",
    }
    records = _signed_get(key, secret, "/fapi/v1/income", params)
    all_records.extend(records)

    # 按symbol汇总
    summary = {}
    for r in all_records:
        sym = r.get("symbol", "UNKNOWN")
        pnl = float(r.get("income", 0))
        if sym not in summary:
            summary[sym] = {"pnl": 0.0, "trades": 0}
        summary[sym]["pnl"] += pnl
        summary[sym]["trades"] += 1

    return summary


def get_funding_income(acct: dict, start_ms: int, end_ms: int) -> float:
    """拉取资金费率收入"""
    vals = dotenv_values(acct["env_file"])
    key = vals.get(acct["key_name"], "")
    secret = vals.get(acct["secret_name"], "")
    if not key or not secret:
        return 0.0

    params = {
        "incomeType": "FUNDING_FEE",
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": "1000",
    }
    records = _signed_get(key, secret, "/fapi/v1/income", params)
    return sum(float(r.get("income", 0)) for r in records)


def get_commission(acct: dict, start_ms: int, end_ms: int) -> float:
    """拉取手续费支出"""
    vals = dotenv_values(acct["env_file"])
    key = vals.get(acct["key_name"], "")
    secret = vals.get(acct["secret_name"], "")
    if not key or not secret:
        return 0.0

    params = {
        "incomeType": "COMMISSION",
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": "1000",
    }
    records = _signed_get(key, secret, "/fapi/v1/income", params)
    return sum(float(r.get("income", 0)) for r in records)


def build_report() -> str:
    now = datetime.now()
    yesterday = now - timedelta(days=1)
    start = datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0)
    end = datetime(now.year, now.month, now.day, 0, 0, 0)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    date_str = yesterday.strftime("%Y-%m-%d")

    lines = [f"📊 <b>合约交易日报 · {date_str}</b>"]
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    grand_total = 0.0
    grand_funding = 0.0
    grand_commission = 0.0
    has_any_trade = False

    for acct in ACCOUNTS:
        summary = get_realized_pnl(acct, start_ms, end_ms)
        funding = get_funding_income(acct, start_ms, end_ms)
        commission = get_commission(acct, start_ms, end_ms)

        lines.append(f"\n<b>【{acct['name']}】</b>")

        if not summary and funding == 0:
            lines.append("  📭 今日无交易")
            continue

        has_any_trade = True
        acct_total = 0.0

        # 按盈亏排序：盈利在前，亏损在后
        sorted_items = sorted(summary.items(), key=lambda x: -x[1]["pnl"])
        for sym, data in sorted_items:
            coin = sym.replace("USDT", "")
            pnl = data["pnl"]
            trades = data["trades"]
            acct_total += pnl
            if pnl >= 0:
                icon = "✅"
            else:
                icon = "❌"
            lines.append(f"  {icon} {coin}  {pnl:+.2f}U  ({trades}笔)")

        if funding != 0:
            lines.append(f"  💸 资金费率: {funding:+.2f}U")
            acct_total += funding

        if commission != 0:
            lines.append(f"  🏷 手续费: {commission:.2f}U")
            acct_total += commission

        acct_icon = "🟢" if acct_total >= 0 else "🔴"
        lines.append(f"  {acct_icon} <b>小计: {acct_total:+.2f}U</b>")
        grand_total += acct_total
        grand_funding += funding
        grand_commission += commission

    lines.append(f"\n━━━━━━━━━━━━━━━━━━━━")
    total_icon = "🟢" if grand_total >= 0 else "🔴"
    lines.append(f"{total_icon} <b>日总盈亏: {grand_total:+.2f}U</b>")

    if not has_any_trade:
        lines.append("\n📭 两个账户今日均无交易记录")

    return "\n".join(lines)


def send_report(text: str):
    if not BOT_TOKEN:
        print("ERROR: PUSH_BOT_TOKEN not set")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        print(f"sent: {resp.status_code}")
    except Exception as e:
        print(f"send failed: {e}")


def _get_reject_section() -> str:
    try:
        import sys
        sys.path.insert(0, "/root/maomao/trader/skills/bull_sniper")
        from reject_tracker import get_daily_report
        return get_daily_report()
    except Exception as e:
        print(f"reject report error: {e}")
        return ""


if __name__ == "__main__":
    report = build_report()
    reject = _get_reject_section()
    if reject:
        report += "\n" + reject
    print(report)
    send_report(report)
