"""
exchange.py — 币安合约连接器 v1.2.2
单例客户端 + 精度修正 + 持仓查询 + 条件单(止盈止损)
"""

import os
import time
import hmac
import hashlib
from urllib.parse import urlencode
from functools import lru_cache
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
from binance.um_futures import UMFutures

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

_client: UMFutures | None = None
_api_key: str = ""
_secret_key: str = ""


def get_client() -> UMFutures:
    global _client, _api_key, _secret_key
    if _client is None:
        _api_key = os.getenv("BINANCE_API_KEY", "")
        _secret_key = os.getenv("BINANCE_SECRET_KEY", "")
        _client = UMFutures(key=_api_key, secret=_secret_key)
    return _client


@lru_cache(maxsize=128)
def get_filters(symbol: str) -> dict:
    """返回 stepSize / tickSize / minNotional"""
    client = get_client()
    info = client.exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            result = {}
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    result["stepSize"] = f["stepSize"]
                elif f["filterType"] == "PRICE_FILTER":
                    result["tickSize"] = f["tickSize"]
                elif f["filterType"] == "MIN_NOTIONAL":
                    result["minNotional"] = float(f.get("notional", 5))
            return result
    raise ValueError(f"Symbol {symbol} not found")


def _step_precision(step: str) -> int:
    d = Decimal(step)
    return max(0, -d.as_tuple().exponent)


def fix_qty(symbol: str, qty: float) -> float:
    filters = get_filters(symbol)
    step = filters["stepSize"]
    result = float(Decimal(str(qty)).quantize(Decimal(step), rounding=ROUND_DOWN))
    return result


def fix_price(symbol: str, price: float) -> float:
    filters = get_filters(symbol)
    tick = filters["tickSize"]
    result = float(Decimal(str(price)).quantize(Decimal(tick), rounding=ROUND_DOWN))
    return result


def check_min_notional(symbol: str, qty: float, price: float) -> None:
    filters = get_filters(symbol)
    min_notional = filters.get("minNotional", 5.0)
    notional = qty * price
    if notional < min_notional:
        raise ValueError(
            f"{symbol} 名义价值 {notional:.2f}U 低于最小要求 {min_notional}U"
        )


def get_mark_price(symbol: str) -> float:
    client = get_client()
    data = client.mark_price(symbol=symbol)
    return float(data["markPrice"])


def set_leverage(symbol: str, leverage: int) -> dict:
    client = get_client()
    return client.change_leverage(symbol=symbol, leverage=leverage)


def set_margin_mode(symbol: str, mode: str) -> dict | None:
    mode_map = {"cross": "CROSSED", "isolated": "ISOLATED"}
    mode = mode_map.get(mode.lower(), mode.upper())
    client = get_client()
    try:
        return client.change_margin_type(symbol=symbol, marginType=mode)
    except Exception as e:
        if "-4046" in str(e):
            return None
        raise


def get_positions(symbol: str | None = None) -> list[dict]:
    client = get_client()
    data = client.get_position_risk(symbol=symbol) if symbol else client.get_position_risk()
    return [p for p in data if float(p.get("positionAmt", 0)) != 0]


# ============================================================
# 条件单 — 走 /fapi/v1/algoOrder 端点（ALGO条件单）
# ============================================================

def place_conditional_order(
    symbol: str,
    side: str,
    order_type: str,
    stop_price: float,
    quantity: float,
    reduce_only: bool = True,
) -> dict:
    """
    下条件单(止盈/止损)，使用 Binance Algo Order 端点
    order_type: STOP_MARKET / TAKE_PROFIT_MARKET
    """
    import requests

    get_client()  # 确保 _api_key/_secret_key 已初始化
    base_url = "https://fapi.binance.com"
    endpoint = "/fapi/v1/algoOrder"

    params = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "algoType": "CONDITIONAL",
        "triggerPrice": str(stop_price),
        "quantity": str(quantity),
        "reduceOnly": "true" if reduce_only else "false",
        "timestamp": str(int(time.time() * 1000)),
    }

    # HMAC SHA256 签名
    query_string = urlencode(params)
    signature = hmac.new(
        _secret_key.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    params["signature"] = signature

    headers = {"X-MBX-APIKEY": _api_key}
    resp = requests.post(f"{base_url}{endpoint}", params=params, headers=headers)
    data = resp.json()

    if resp.status_code != 200:
        code = data.get("code", "?")
        msg = data.get("msg", str(data))
        raise Exception(f"({resp.status_code}, {code}, '{msg}')")

    return data


def ping() -> str:
    try:
        client = get_client()
        client.ping()
        return "✅ 币安合约连接正常"
    except Exception as e:
        return f"❌ 连接失败: {e}"
