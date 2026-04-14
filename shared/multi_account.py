"""
multi_account.py — 多账户查询模块
只读查询，不执行交易。交易仍走各自的 exchange.py。
"""

import os
import hmac
import hashlib
import time

import requests
from dotenv import load_dotenv

load_dotenv("/root/maomao/.env")

ACCOUNTS = {
    "乌鸦": {
        "api_key_env": "BINANCE_API_KEY",
        "secret_env": "BINANCE_SECRET_KEY",
        "label": "币安1（乌鸦）",
    },
    "震天响": {
        "api_key_env": "BINANCE2_API_KEY",
        "secret_env": "BINANCE2_SECRET_KEY",
        "label": "币安2（震天响）",
    },
}

BASE_URL = "https://fapi.binance.com"


def _sign(params: dict, secret: str) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + f"&signature={sig}"


def _get_keys(account_name: str) -> tuple:
    acct = ACCOUNTS[account_name]
    key = os.getenv(acct["api_key_env"], "")
    secret = os.getenv(acct["secret_env"], "")
    return key, secret


def get_positions(account_name: str = None) -> dict:
    """查询一个或全部账户的持仓"""
    targets = {account_name: ACCOUNTS[account_name]} if account_name else ACCOUNTS
    result = {}

    for name, acct in targets.items():
        key, secret = _get_keys(name)
        if not key:
            result[name] = {"label": acct["label"], "error": "API key未配置"}
            continue

        params = {"timestamp": int(time.time() * 1000)}
        signed = _sign(params, secret)
        headers = {"X-MBX-APIKEY": key}

        try:
            resp = requests.get(
                f"{BASE_URL}/fapi/v3/positionRisk?{signed}",
                headers=headers, timeout=10,
            )
            data = resp.json()
            positions = [
                p for p in data
                if isinstance(p, dict) and float(p.get("positionAmt", 0)) != 0
            ]
            result[name] = {"label": acct["label"], "positions": positions}
        except Exception as e:
            result[name] = {"label": acct["label"], "error": str(e)}

    return result


def get_balance(account_name: str = None) -> dict:
    """查询一个或全部账户的余额"""
    targets = {account_name: ACCOUNTS[account_name]} if account_name else ACCOUNTS
    result = {}

    for name, acct in targets.items():
        key, secret = _get_keys(name)
        if not key:
            result[name] = {"label": acct["label"], "error": "API key未配置"}
            continue

        params = {"timestamp": int(time.time() * 1000)}
        signed = _sign(params, secret)
        headers = {"X-MBX-APIKEY": key}

        try:
            resp = requests.get(
                f"{BASE_URL}/fapi/v2/account?{signed}",
                headers=headers, timeout=10,
            )
            data = resp.json()
            result[name] = {
                "label": acct["label"],
                "total": data.get("totalWalletBalance", "0"),
                "available": data.get("availableBalance", "0"),
                "upnl": data.get("totalUnrealizedProfit", "0"),
            }
        except Exception as e:
            result[name] = {"label": acct["label"], "error": str(e)}

    return result


def format_all_positions() -> str:
    """格式化全部账户持仓，返回可直接回复的文本"""
    data = get_positions()
    lines = []
    for name, info in data.items():
        lines.append(f"📋 {info['label']}")
        if "error" in info:
            lines.append(f"  ⚠️ {info['error']}")
            continue
        if not info["positions"]:
            lines.append("  当前无持仓")
            continue
        for p in info["positions"]:
            amt = float(p["positionAmt"])
            side = "多" if amt > 0 else "空"
            entry = float(p["entryPrice"])
            upnl = float(p["unRealizedProfit"])
            lev = p.get("leverage", "?")
            lines.append(
                f"  {p['symbol'].replace('USDT','')} {side} {lev}x "
                f"入场:{entry:.4f} 浮盈:{upnl:+.2f}U"
            )
    return "\n".join(lines)


def format_all_balances() -> str:
    """格式化全部账户余额"""
    data = get_balance()
    lines = []
    for name, info in data.items():
        if "error" in info:
            lines.append(f"💰 {info['label']}: ⚠️ {info['error']}")
            continue
        upnl = float(info.get("upnl", 0))
        lines.append(
            f"💰 {info['label']}: "
            f"余额:{float(info['total']):.2f}U  "
            f"可用:{float(info['available']):.2f}U  "
            f"浮盈:{upnl:+.2f}U"
        )
    return "\n".join(lines)
