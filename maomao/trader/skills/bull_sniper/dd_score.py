"""
DD 因子：有方向的量能（v3.5）
数据源：/fapi/v1/aggTrades  5 分钟窗口聚合
taker_ratio = 主动买 USDT / 主动卖 USDT
档位：≥2.5→15 / ≥2.0→12 / ≥1.5→8 / ≥1.2→4 / <1.2→0
"""
import logging
import time

import requests

logger = logging.getLogger("dd_score")

FAPI_BASE = "https://fapi.binance.com"
_WINDOW_SEC = 300  # 5 分钟


def score_dd(symbol: str, cfg: dict) -> tuple[int, str]:
    """返回 (分值, 原因描述)；API 失败 fail-open 返回 (0, '')"""
    try:
        now_ms   = int(time.time() * 1000)
        start_ms = now_ms - _WINDOW_SEC * 1000

        trades = _fetch_agg_trades(symbol, start_ms, now_ms)

        if not trades:
            return 0, ""  # 窗口无成交

        buy_usdt  = 0.0
        sell_usdt = 0.0
        for t in trades:
            val = float(t["p"]) * float(t["q"])
            if t["m"]:          # isBuyerMaker=True → taker 是卖方
                sell_usdt += val
            else:               # isBuyerMaker=False → taker 是买方
                buy_usdt  += val

        if sell_usdt == 0:
            if buy_usdt >= 100_000:
                return 15, f"DD极端买方主导 买{buy_usdt/1000:.0f}k"
            return 0, ""

        if sell_usdt == 0:
            return 0, ""

        ratio = buy_usdt / sell_usdt

        if ratio >= 2.5:
            return 15, f"DD超强买压 {ratio:.2f}x"
        if ratio >= 2.0:
            return 12, f"DD强买压 {ratio:.2f}x"
        if ratio >= 1.5:
            return  8, f"DD买压偏强 {ratio:.2f}x"
        if ratio >= 1.2:
            return  4, f"DD温和买压 {ratio:.2f}x"
        return 0, ""

    except Exception as e:
        logger.debug(f"[DD] {symbol} 跳过: {e}")
        return 0, ""  # fail-open


def _fetch_agg_trades(symbol: str, start_ms: int, end_ms: int) -> list:
    resp = requests.get(
        f"{FAPI_BASE}/fapi/v1/aggTrades",
        params={"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": 1000},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()
