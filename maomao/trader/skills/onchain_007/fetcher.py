"""GeckoTerminal trending_pools 拉数据 + 标准化字段"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger("onchain_007.fetcher")

BASE = "https://api.geckoterminal.com/api/v2"
TIMEOUT = 12


def fetch_trending(network: str, limit: int = 20, retries: int = 3) -> list[dict]:
    """拉一条链的 trending pools，返回标准化字段列表（429 自动退避重试）。"""
    url = f"{BASE}/networks/{network}/trending_pools"
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=TIMEOUT, params={"include": "base_token"},
                             headers={"Accept": "application/json"})
            if r.status_code == 429:
                wait = 3 + attempt * 2
                logger.warning(f"[{network}] 429 limit, retry in {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            last_err = e
            time.sleep(2)
    else:
        raise last_err if last_err else RuntimeError("fetch failed")

    # 建 token 索引（included 段）
    tokens: dict[str, dict] = {}
    for inc in data.get("included", []):
        if inc.get("type") == "token":
            tokens[inc["id"]] = inc.get("attributes", {})

    pools: list[dict] = []
    for item in data.get("data", []):
        attrs = item.get("attributes", {}) or {}
        rel = item.get("relationships", {}) or {}
        base_tok_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
        tok = tokens.get(base_tok_id, {}) or {}

        try:
            mcap = float(attrs.get("market_cap_usd") or attrs.get("fdv_usd") or 0)
            liq = float(attrs.get("reserve_in_usd") or 0)
            vol = attrs.get("volume_usd") or {}
            chg = attrs.get("price_change_percentage") or {}
            vol_h24 = float(vol.get("h24") or 0)
            chg_h1 = float(chg.get("h1") or 0)
            chg_h24 = float(chg.get("h24") or 0)
            created_at = attrs.get("pool_created_at", "") or ""
            age_h = 0.0
            if created_at:
                t = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                age_h = (datetime.now(timezone.utc) - t).total_seconds() / 3600
        except Exception as e:
            logger.debug(f"skip pool parse fail: {e}")
            continue

        pools.append({
            "network":       network,
            "symbol":        (tok.get("symbol") or "?").strip()[:12],
            "name":          (tok.get("name") or "?")[:40],
            "marketcap_usd": mcap,
            "liquidity_usd": liq,
            "volume_h24":    vol_h24,
            "change_h1":     chg_h1,
            "change_h24":    chg_h24,
            "age_hours":     age_h,
            "pair_id":       item.get("id", ""),
        })
        if len(pools) >= limit:
            break

    logger.info(f"[{network}] fetched {len(pools)} pools")
    return pools
