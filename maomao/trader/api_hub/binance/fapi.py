# api_hub/binance/fapi.py
# 币安 USDT-M 合约 fapi 端点封装
# 公开端点 = 无签名；签名端点 = 显式传 api_key + secret

from .._base import http
from .._base.auth import binance_sign as _binance_sign

BASE = "https://fapi.binance.com"


# ─────────────── 公开端点 ───────────────

def get_ticker_price(symbol: str = None) -> dict | list:
    """GET /fapi/v1/ticker/price 标记价/最新价。
    symbol=None 返回全市场列表。"""
    params = {"symbol": symbol} if symbol else None
    return http.get(f"{BASE}/fapi/v1/ticker/price", params=params)


def get_ticker_24hr(symbol: str = None) -> dict | list:
    """GET /fapi/v1/ticker/24hr 24h 行情。"""
    params = {"symbol": symbol} if symbol else None
    return http.get(f"{BASE}/fapi/v1/ticker/24hr", params=params)


def get_premium_index(symbol: str = None) -> dict | list:
    """GET /fapi/v1/premiumIndex 标记价 + 资金费率。"""
    params = {"symbol": symbol} if symbol else None
    return http.get(f"{BASE}/fapi/v1/premiumIndex", params=params)


def get_klines(symbol: str, interval: str, limit: int = 500,
               start_time: int = None, end_time: int = None) -> list:
    """GET /fapi/v1/klines K 线。
    interval: 1m/3m/5m/15m/30m/1h/2h/4h/6h/8h/12h/1d/3d/1w/1M"""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_time:
        params["startTime"] = start_time
    if end_time:
        params["endTime"] = end_time
    return http.get(f"{BASE}/fapi/v1/klines", params=params)


def get_exchange_info() -> dict:
    """GET /fapi/v1/exchangeInfo 全市场合约元数据（stepSize/tickSize/minNotional 等）。
    注意：响应大（几 MB），调用方应自己缓存。"""
    return http.get(f"{BASE}/fapi/v1/exchangeInfo")


def get_open_interest(symbol: str) -> dict:
    """GET /fapi/v1/openInterest 当前未平仓合约量。"""
    return http.get(f"{BASE}/fapi/v1/openInterest", params={"symbol": symbol})


def get_open_interest_hist(symbol: str, period: str = "5m", limit: int = 30) -> list:
    """GET /futures/data/openInterestHist OI 历史。
    period: 5m/15m/30m/1h/2h/4h/6h/12h/1d"""
    return http.get(f"{BASE}/futures/data/openInterestHist",
                    params={"symbol": symbol, "period": period, "limit": limit})


def get_top_lsr_account(symbol: str, period: str = "5m", limit: int = 30) -> list:
    """GET /futures/data/topLongShortAccountRatio 大户多空账户比。"""
    return http.get(f"{BASE}/futures/data/topLongShortAccountRatio",
                    params={"symbol": symbol, "period": period, "limit": limit})


def get_depth(symbol: str, limit: int = 100) -> dict:
    """GET /fapi/v1/depth 盘口。limit: 5/10/20/50/100/500/1000"""
    return http.get(f"{BASE}/fapi/v1/depth",
                    params={"symbol": symbol, "limit": limit})


def get_agg_trades(symbol: str, limit: int = 100,
                   from_id: int = None, start_time: int = None, end_time: int = None) -> list:
    """GET /fapi/v1/aggTrades 聚合大单。"""
    params = {"symbol": symbol, "limit": limit}
    if from_id:
        params["fromId"] = from_id
    if start_time:
        params["startTime"] = start_time
    if end_time:
        params["endTime"] = end_time
    return http.get(f"{BASE}/fapi/v1/aggTrades", params=params)


# ─────────────── 签名端点（需 api_key + secret）───────────────

def _signed_headers(api_key: str) -> dict:
    return {"X-MBX-APIKEY": api_key, "Content-Type": "application/x-www-form-urlencoded"}


def get_account(api_key: str, secret: str) -> dict:
    """GET /fapi/v2/account 账户信息（合约余额、持仓汇总）。"""
    params = _binance_sign({}, secret)
    return http.get(f"{BASE}/fapi/v2/account",
                    params=params, headers=_signed_headers(api_key))


def get_position_risk(api_key: str, secret: str, symbol: str = None) -> list:
    """GET /fapi/v2/positionRisk 持仓详情（含强平价）。"""
    params = {"symbol": symbol} if symbol else {}
    params = _binance_sign(params, secret)
    return http.get(f"{BASE}/fapi/v2/positionRisk",
                    params=params, headers=_signed_headers(api_key))


def place_order(api_key: str, secret: str, **params) -> dict:
    """POST /fapi/v1/order 普通下单。
    必填: symbol, side(BUY/SELL), type(MARKET/LIMIT/STOP/...), quantity
    可选: positionSide(BOTH/LONG/SHORT), price, stopPrice, timeInForce, reduceOnly..."""
    params = _binance_sign(params, secret)
    return http.post(f"{BASE}/fapi/v1/order",
                     params=params, headers=_signed_headers(api_key))


def cancel_order(api_key: str, secret: str, symbol: str,
                 order_id: int = None, client_order_id: str = None) -> dict:
    """DELETE /fapi/v1/order 撤单。order_id 和 client_order_id 二选一。"""
    params = {"symbol": symbol}
    if order_id:
        params["orderId"] = order_id
    if client_order_id:
        params["origClientOrderId"] = client_order_id
    params = _binance_sign(params, secret)
    return http._request("DELETE", f"{BASE}/fapi/v1/order",
                         params=params, headers=_signed_headers(api_key))


def cancel_all_open_orders(api_key: str, secret: str, symbol: str) -> dict:
    """DELETE /fapi/v1/allOpenOrders 撤掉某币所有普通挂单（不影响 algo）。"""
    params = _binance_sign({"symbol": symbol}, secret)
    return http._request("DELETE", f"{BASE}/fapi/v1/allOpenOrders",
                         params=params, headers=_signed_headers(api_key))


def place_algo_order(api_key: str, secret: str, **params) -> dict:
    """POST /fapi/v1/algoOrder 条件单（止损/止盈，作为暗单不冻结保证金）。
    必填: symbol, side, type(STOP_MARKET/TAKE_PROFIT_MARKET), stopPrice, quantity 或 closePosition"""
    params = _binance_sign(params, secret)
    return http.post(f"{BASE}/fapi/v1/algoOrder",
                     params=params, headers=_signed_headers(api_key))


def cancel_algo_order(api_key: str, secret: str, symbol: str, algo_id: int) -> dict:
    """DELETE /fapi/v1/algoOrder 撤单条件单。"""
    params = _binance_sign({"symbol": symbol, "algoId": algo_id}, secret)
    return http._request("DELETE", f"{BASE}/fapi/v1/algoOrder",
                         params=params, headers=_signed_headers(api_key))


def get_open_algo_orders(api_key: str, secret: str, symbol: str = None) -> list:
    """GET /fapi/v1/openAlgoOrders 当前所有暗单条件单（止损/止盈）。"""
    params = {"symbol": symbol} if symbol else {}
    params = _binance_sign(params, secret)
    return http.get(f"{BASE}/fapi/v1/openAlgoOrders",
                    params=params, headers=_signed_headers(api_key))
