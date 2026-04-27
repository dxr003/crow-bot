# api_hub/data/geckoterminal.py
# GeckoTerminal API v2 封装
# 文档: https://api.geckoterminal.com/docs/index.html
# 限速: 30 calls/min（公开端点，无 key）

import logging
import time
from .._base import http
from .._base.errors import RateLimitError, ApiError

BASE = "https://api.geckoterminal.com/api/v2"
DEFAULT_TIMEOUT = 12

logger = logging.getLogger("api_hub.geckoterminal")


def _get_with_429_retry(url: str, params: dict = None, retries: int = 3) -> dict:
    """gecko 触发 429 时自动退避重试（http.py 默认不重试 429，gecko 例外）"""
    last_err = None
    for attempt in range(retries):
        try:
            return http.get(url, params=params, headers={"Accept": "application/json"},
                            timeout=DEFAULT_TIMEOUT)
        except RateLimitError as e:
            wait = 3 + attempt * 2  # 3s, 5s, 7s
            logger.warning(f"[gecko] 429, retry {attempt+1}/{retries} in {wait}s")
            time.sleep(wait)
            last_err = e
        except ApiError as e:
            last_err = e
            time.sleep(2)
    raise last_err if last_err else ApiError("gecko fetch failed")


def get_trending_pools(network: str, include: str = "base_token") -> dict:
    """GET /networks/{network}/trending_pools 拉某条链的 trending pool 列表。
    network: solana / bsc / eth / base / arbitrum 等
    返回原始 JSON：{data: [...], included: [...]}"""
    url = f"{BASE}/networks/{network}/trending_pools"
    return _get_with_429_retry(url, params={"include": include})


def get_pool(network: str, address: str, include: str = "base_token,quote_token") -> dict:
    """GET /networks/{network}/pools/{address} 单个池子详情（V2 备用）"""
    url = f"{BASE}/networks/{network}/pools/{address}"
    return _get_with_429_retry(url, params={"include": include})


def search_pools(query: str, network: str = None) -> dict:
    """GET /search/pools 全网/指定链搜索池子（V2 备用）"""
    url = f"{BASE}/search/pools"
    params = {"query": query}
    if network:
        params["network"] = network
    return _get_with_429_retry(url, params=params)
