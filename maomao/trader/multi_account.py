"""
多账户查询 — 玄玄总控视图
支持币安1(主)、币安2(七星)，未来可扩展币安3
"""
import os
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode
from pathlib import Path
from dotenv import load_dotenv
from binance.um_futures import UMFutures

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


def _load_client(acct: dict) -> tuple:
    from dotenv import dotenv_values
    vals = dotenv_values(acct["env_file"])
    key = vals.get(acct["key_name"], "")
    secret = vals.get(acct["secret_name"], "")
    client = UMFutures(key=key, secret=secret)
    return client, key, secret


def _sign(secret: str, params: dict) -> str:
    query = urlencode(params)
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def get_all_positions() -> list:
    results = []
    for acct in ACCOUNTS:
        try:
            client, _, _ = _load_client(acct)
            data = client.get_position_risk()
            positions = [p for p in data if float(p.get("positionAmt", 0)) != 0]
            for p in positions:
                p["_account"] = acct["name"]
            results.extend(positions)
        except Exception as e:
            results.append({"_account": acct["name"], "_error": str(e)})
    return results


def get_all_balances() -> list:
    results = []
    for acct in ACCOUNTS:
        try:
            client, key, secret = _load_client(acct)
            info = client.account()
            bal = {
                "name": acct["name"],
                "futures": float(info.get("totalWalletBalance", 0)),
                "futures_avail": float(info.get("availableBalance", 0)),
                "futures_upnl": float(info.get("totalUnrealizedProfit", 0)),
            }

            headers = {"X-MBX-APIKEY": key}

            # 现货账户
            p = {"timestamp": str(int(time.time() * 1000))}
            p["signature"] = _sign(secret, p)
            r = requests.get("https://api.binance.com/api/v3/account",
                             params=p, headers=headers, timeout=10)
            if r.status_code == 200:
                balances = r.json().get("balances", [])
                bal["spot"] = {
                    b["asset"]: float(b["free"]) + float(b["locked"])
                    for b in balances
                    if float(b["free"]) + float(b["locked"]) > 0
                }
            else:
                bal["spot"] = {}

            # 资金账户
            p2 = {"timestamp": str(int(time.time() * 1000))}
            p2["signature"] = _sign(secret, p2)
            r2 = requests.post("https://api.binance.com/sapi/v1/asset/get-funding-asset",
                               params=p2, headers=headers, timeout=10)
            if r2.status_code == 200 and isinstance(r2.json(), list):
                bal["funding"] = {
                    i["asset"]: float(i.get("free", 0)) + float(i.get("locked", 0))
                    for i in r2.json()
                    if float(i.get("free", 0)) + float(i.get("locked", 0)) > 0
                }
            else:
                bal["funding"] = {}

            results.append(bal)
        except Exception as e:
            results.append({"name": acct["name"], "error": str(e)})
    return results
