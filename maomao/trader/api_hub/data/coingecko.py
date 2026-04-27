# api_hub/data/coingecko.py
# CoinGecko API v3 封装（公开端点，免 key）
# 文档: https://docs.coingecko.com/v3.0.1/reference/introduction
# 限速: 30 calls/min（免费层）

import logging
import time
from .._base import http
from .._base.errors import RateLimitError, ApiError

BASE = "https://api.coingecko.com/api/v3"
DEFAULT_TIMEOUT = 10

logger = logging.getLogger("api_hub.coingecko")


def _get_with_429_retry(url: str, params: dict = None, retries: int = 3):
    """coingecko 触发 429 时自动退避重试（http.py 默认不重试 429，coingecko 例外）"""
    last_err = None
    for attempt in range(retries):
        try:
            return http.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        except RateLimitError as e:
            wait = 3 + attempt * 2
            logger.warning(f"[coingecko] 429, retry {attempt+1}/{retries} in {wait}s")
            time.sleep(wait)
            last_err = e
        except ApiError as e:
            last_err = e
            time.sleep(2)
    raise last_err if last_err else ApiError("coingecko fetch failed")


def get_top_coins(top_n: int = 50, vs_currency: str = "usd",
                  order: str = "market_cap_desc", page: int = 1) -> list:
    """GET /coins/markets 市值排行榜。
    返回 list of dict，含 symbol/name/market_cap/current_price 等"""
    return _get_with_429_retry(f"{BASE}/coins/markets", params={
        "vs_currency": vs_currency,
        "order": order,
        "per_page": top_n,
        "page": page,
    })


def get_simple_price(ids: list[str] | str, vs_currency: str = "usd") -> dict:
    """GET /simple/price 单/多个币种当前价格（按 coingecko id 查，如 'bitcoin'）"""
    if isinstance(ids, list):
        ids = ",".join(ids)
    return _get_with_429_retry(f"{BASE}/simple/price", params={
        "ids": ids,
        "vs_currencies": vs_currency,
    })
