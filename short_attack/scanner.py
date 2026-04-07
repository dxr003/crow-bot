"""
scanner.py — 扫描币安合约暴涨榜
只负责获取数据，不做任何状态判断
"""
import os
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode

API_KEY    = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BASE_FAPI  = "https://fapi.binance.com"
BASE_API   = "https://api.binance.com"


def _sign(params: dict) -> str:
    query = urlencode(params)
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()


def get_ticker_24h() -> list[dict]:
    """获取所有合约24h行情，只返回 TRADING 状态的 USDT 合约"""
    try:
        resp = requests.get(f"{BASE_FAPI}/fapi/v1/ticker/24hr", timeout=10)
        resp.raise_for_status()
        tickers = resp.json()
    except Exception as e:
        raise RuntimeError(f"获取ticker失败: {e}")

    # 获取合约状态列表，过滤掉 SETTLING / DELIVERING 等
    try:
        info_resp = requests.get(f"{BASE_FAPI}/fapi/v1/exchangeInfo", timeout=10)
        info_resp.raise_for_status()
        trading_symbols = {
            s["symbol"]
            for s in info_resp.json().get("symbols", [])
            if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
               and s.get("contractType") == "PERPETUAL"
        }
    except Exception as e:
        raise RuntimeError(f"获取exchangeInfo失败: {e}")

    result = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if symbol not in trading_symbols:
            continue
        try:
            change_pct = float(t.get("priceChangePercent", 0))
            last_price = float(t.get("lastPrice", 0))
            volume_usdt = float(t.get("quoteVolume", 0))
            if last_price <= 0:
                continue
            result.append({
                "symbol":      symbol,
                "price":       last_price,
                "change_pct":  change_pct,
                "volume_usdt": volume_usdt,
            })
        except (ValueError, TypeError):
            continue

    return result


def get_funding_rate(symbol: str) -> float:
    """获取当前资金费率"""
    try:
        resp = requests.get(
            f"{BASE_FAPI}/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=5
        )
        resp.raise_for_status()
        return float(resp.json().get("lastFundingRate", 0)) * 100  # 转为百分比
    except Exception:
        return 0.0


def get_open_interest(symbol: str) -> dict:
    """获取持仓量及变化"""
    try:
        # 当前OI
        resp = requests.get(
            f"{BASE_FAPI}/fapi/v1/openInterest",
            params={"symbol": symbol},
            timeout=5
        )
        resp.raise_for_status()
        oi_now = float(resp.json().get("openInterest", 0))

        # 24h前OI（用统计接口）
        params = {"symbol": symbol, "period": "1h", "limit": 25}
        hist_resp = requests.get(
            f"{BASE_FAPI}/futures/data/openInterestHist",
            params=params,
            timeout=5
        )
        hist_resp.raise_for_status()
        hist = hist_resp.json()
        if hist and len(hist) >= 2:
            oi_24h_ago = float(hist[0].get("sumOpenInterest", oi_now))
            oi_change_pct = (oi_now - oi_24h_ago) / oi_24h_ago * 100 if oi_24h_ago else 0
        else:
            oi_change_pct = 0.0

        return {"oi": oi_now, "oi_change_pct": round(oi_change_pct, 2)}
    except Exception:
        return {"oi": 0.0, "oi_change_pct": 0.0}


def get_signal_data(symbol: str) -> dict:
    """获取持仓信号推送所需的辅助数据（资金费率/OI/成交量）"""
    funding = get_funding_rate(symbol)
    oi_data = get_open_interest(symbol)
    return {
        "funding_rate":  round(funding, 4),
        "oi_change_pct": oi_data["oi_change_pct"],
    }
