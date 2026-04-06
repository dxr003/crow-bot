"""
exchange.py — 币安合约连接器 v1.2.3
单例客户端 + 精度修正 + 持仓查询 + 条件单(algoOrder)
"""

import os
import time
import hmac
import hashlib
import requests
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
    return float(Decimal(str(qty)).quantize(Decimal(step), rounding=ROUND_DOWN))


def fix_price(symbol: str, price: float) -> float:
    filters = get_filters(symbol)
    tick = filters["tickSize"]
    return float(Decimal(str(price)).quantize(Decimal(tick), rounding=ROUND_DOWN))


def check_min_notional(symbol: str, qty: float, price: float) -> None:
    filters = get_filters(symbol)
    min_notional = filters.get("minNotional", 5.0)
    notional = qty * price
    if notional < min_notional:
        raise ValueError(f"{symbol} 名义价值 {notional:.2f}U < 最小 {min_notional}U")


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
# 条件单 — POST /fapi/v1/algoOrder
# 2025-12-09起 STOP_MARKET/TAKE_PROFIT_MARKET 必须走此端点
# ============================================================

def _sign(params: dict) -> str:
    """HMAC SHA256 签名"""
    query = urlencode(params)
    return hmac.new(
        _secret_key.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def place_conditional_order(
    symbol: str,
    side: str,
    order_type: str,
    trigger_price: float,
    quantity: float = 0,
    reduce_only: bool = True,
    close_position: bool = False,
) -> dict:
    """
    下条件单(止盈/止损)
    order_type: STOP_MARKET / TAKE_PROFIT_MARKET / TRAILING_STOP_MARKET
    trigger_price: 触发价格
    close_position: True=触发时平掉该方向全部仓位(此时不传quantity)
    """
    get_client()  # 确保 credentials 已初始化

    params = {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "triggerPrice": str(trigger_price),
        "timestamp": str(int(time.time() * 1000)),
    }

    if close_position:
        params["closePosition"] = "true"
    else:
        params["quantity"] = str(quantity)
        if reduce_only:
            params["reduceOnly"] = "true"

    params["signature"] = _sign(params)

    headers = {"X-MBX-APIKEY": _api_key}
    resp = requests.post(
        "https://fapi.binance.com/fapi/v1/algoOrder",
        params=params,
        headers=headers,
    )
    data = resp.json()

    if resp.status_code != 200:
        code = data.get("code", "?")
        msg = data.get("msg", str(data))
        raise Exception(f"Algo Order 失败: ({code}) {msg}")

    return data


def cancel_all_orders(symbol: str) -> int:
    """撤销某币种所有普通挂单 + algoOrder 条件单（止盈/止损），返回撤销数量"""
    get_client()  # 确保 credentials 已初始化
    headers = {"X-MBX-APIKEY": _api_key}
    cancelled = 0

    # 1. 普通挂单（旧版 LIMIT/STOP_MARKET 等）
    try:
        params = {"symbol": symbol, "timestamp": str(int(time.time() * 1000))}
        params["signature"] = _sign(params)
        requests.delete("https://fapi.binance.com/fapi/v1/allOpenOrders",
                        params=params, headers=headers)
        cancelled += 1
    except Exception:
        pass

    # 2. algoOrder 条件单（TP/SL — GET /fapi/v1/openAlgoOrders 再逐个 DELETE）
    try:
        p = {"timestamp": str(int(time.time() * 1000))}
        p["signature"] = _sign(p)
        resp = requests.get("https://fapi.binance.com/fapi/v1/openAlgoOrders",
                            params=p, headers=headers)
        all_algo = resp.json() if resp.status_code == 200 else []
        for o in (all_algo if isinstance(all_algo, list) else []):
            if o.get("symbol") != symbol:
                continue
            algo_id = o.get("algoId")
            if not algo_id:
                continue
            dp = {"algoId": str(algo_id), "timestamp": str(int(time.time() * 1000))}
            dp["signature"] = _sign(dp)
            requests.delete("https://fapi.binance.com/fapi/v1/algoOrder",
                            params=dp, headers=headers)
            cancelled += 1
    except Exception:
        pass

    return cancelled


def get_balance() -> dict:
    """获取合约账户余额"""
    client = get_client()
    info = client.futures_account()
    return {
        "total":     float(info["totalWalletBalance"]),
        "available": float(info["availableBalance"]),
        "upnl":      float(info["totalUnrealizedProfit"]),
    }


def ping() -> str:
    try:
        client = get_client()
        client.ping()
        return "✅ 币安合约连接正常"
    except Exception as e:
        return f"❌ 连接失败: {e}"
