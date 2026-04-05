"""
exchange.py — 币安合约连接器
单例客户端 + 精度修正 + 持仓查询
"""

import os
from functools import lru_cache
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
from binance.um_futures import UMFutures

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

_client: UMFutures | None = None


def get_client() -> UMFutures:
    global _client
    if _client is None:
        api_key = os.getenv("BINANCE_API_KEY", "")
        secret_key = os.getenv("BINANCE_SECRET_KEY", "")
        _client = UMFutures(key=api_key, secret=secret_key)
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
    """从 stepSize/tickSize 字符串推算小数位数"""
    d = Decimal(step)
    return max(0, -d.as_tuple().exponent)


def fix_qty(symbol: str, qty: float) -> float:
    filters = get_filters(symbol)
    step = filters["stepSize"]
    precision = _step_precision(step)
    result = float(Decimal(str(qty)).quantize(Decimal(step), rounding=ROUND_DOWN))
    return result


def fix_price(symbol: str, price: float) -> float:
    filters = get_filters(symbol)
    tick = filters["tickSize"]
    result = float(Decimal(str(price)).quantize(Decimal(tick), rounding=ROUND_DOWN))
    return result


def check_min_notional(symbol: str, qty: float, price: float) -> None:
    """名义价值不足时抛出 ValueError"""
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
    """mode: 'CROSSED'/'cross' 或 'ISOLATED'/'isolated'，-4046(已是该模式)静默忽略"""
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
    """返回 positionAmt != 0 的持仓列表"""
    client = get_client()
    data = client.get_position_risk(symbol=symbol) if symbol else client.get_position_risk()
    return [p for p in data if float(p.get("positionAmt", 0)) != 0]


def ping() -> str:
    try:
        client = get_client()
        client.ping()
        return "✅ 币安合约连接正常"
    except Exception as e:
        return f"❌ 连接失败: {e}"
