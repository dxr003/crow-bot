"""
news_score.py — E因子：社交热度+聪明钱评分模块 v3.1
数据源：币安 Web3 Skills API（免费无认证）

社交热度（互斥取最高）：
  Positive        → +2
  Positive+>10万   → +5（替代+2）

聪明钱（三档互斥+减分标签）：
  buy+valid                  → +4
  buy+valid+count≥3          → +6（替代+4）
  buy+valid+value≥$5万       → +10（替代+6）
  Smart Money Remove Holdings → -10
  Insider Wash Trading        → -8

2026-04-13 v1 搭建
2026-04-13 v3.1 重构：拆分get_social_score/get_smart_money_score
"""
import time
import logging
import requests

logger = logging.getLogger("bull_sniper.news_score")

BASE = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct"
HEADERS_GET = {
    "Accept-Encoding": "identity",
    "User-Agent": "binance-web3/2.1 (Skill)",
}
HEADERS_POST = {
    "Content-Type": "application/json",
    "Accept-Encoding": "identity",
    "User-Agent": "binance-web3/1.1 (Skill)",
}

_cache: dict = {}
CACHE_TTL = 300


def _cached_get(key: str, fetcher):
    now = time.time()
    if key in _cache and now - _cache[key][0] < CACHE_TTL:
        return _cache[key][1]
    data = fetcher()
    if data is not None:
        _cache[key] = (now, data)
    return data


def _fetch_social_hype(chain_id: str) -> list:
    try:
        url = (
            f"{BASE}/buw/wallet/market/token/pulse/social/hype/"
            f"rank/leaderboard/ai?chainId={chain_id}"
            f"&sentiment=All&socialLanguage=ALL&targetLanguage=en&timeRange=1"
        )
        resp = requests.get(url, headers=HEADERS_GET, timeout=10)
        if resp.status_code == 200:
            body = resp.json()
            data = body.get("data", {})
            if isinstance(data, dict):
                return data.get("leaderBoardList", data.get("list", []))
            return []
    except Exception as e:
        logger.debug(f"[news_score] social hype {chain_id} 异常: {e}")
    return []


def _fetch_smart_money(chain_id: str) -> list:
    try:
        url = f"{BASE}/buw/wallet/web/signal/smart-money/ai"
        resp = requests.post(url, headers=HEADERS_POST, json={
            "smartSignalType": "",
            "page": 1,
            "pageSize": 100,
            "chainId": chain_id,
        }, timeout=10)
        if resp.status_code == 200:
            body = resp.json()
            data = body.get("data", [])
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug(f"[news_score] smart money {chain_id} 异常: {e}")
    return []


def _get_all_social_hype() -> list:
    def fetch():
        bsc = _fetch_social_hype("56")
        sol = _fetch_social_hype("CT_501")
        return bsc + sol
    return _cached_get("social_hype_all", fetch) or []


def _get_all_smart_money() -> list:
    def fetch():
        bsc = _fetch_smart_money("56")
        sol = _fetch_smart_money("CT_501")
        return bsc + sol
    return _cached_get("smart_money_all", fetch) or []


def get_social_score(symbol: str) -> dict:
    """
    E因子-社交部分：互斥取最高
    返回: {"score": int, "reason": str}
    """
    ticker = symbol.replace("USDT", "").replace("BUSD", "").upper()
    try:
        hype_list = _get_all_social_hype()
        for item in hype_list:
            meta = item.get("metaInfo") or {}
            hype_info = item.get("socialHypeInfo") or {}
            item_symbol = (meta.get("symbol") or "").upper()
            if item_symbol != ticker:
                continue
            sentiment = hype_info.get("sentiment", "")
            hype_val = float(hype_info.get("socialHype", 0))
            if sentiment == "Positive":
                if hype_val > 100_000:
                    return {"score": 5, "reason": f"社交爆发hype{hype_val/1000:.0f}K"}
                return {"score": 2, "reason": "社交正面"}
            break
    except Exception as e:
        logger.debug(f"[social_score] {ticker} 异常: {e}")
    return {"score": 0, "reason": ""}


def get_smart_money_score(symbol: str) -> dict:
    """
    E因子-聪明钱部分：三档互斥+减分标签
    返回: {"score": int, "reason": str}
    """
    ticker = symbol.replace("USDT", "").replace("BUSD", "").upper()
    score = 0
    reason = ""

    try:
        sm_list = _get_all_smart_money()
        for sig in sm_list:
            sig_ticker = (sig.get("ticker") or "").upper()
            if sig_ticker != ticker:
                continue

            direction = sig.get("direction", "")
            status = sig.get("status", "")
            tags = sig.get("tokenTag", {})

            all_tag_names = []
            for tag_group in tags.values():
                if isinstance(tag_group, list):
                    for t in tag_group:
                        all_tag_names.append(t.get("tagName", ""))

            has_reduce = any("Smart Money Remove Holdings" in t for t in all_tag_names)
            has_wash = any("Insider Wash Trading" in t for t in all_tag_names)

            if has_reduce:
                score -= 10
                reason = "聪明钱已减仓-10"
            if has_wash:
                score -= 8
                reason = ("内部对敲-8" if not has_reduce
                          else "聪明钱减仓-10+对敲-8")

            if direction == "buy" and status in ("active", "valid"):
                sm_count = int(sig.get("smartMoneyCount", 0))
                total_value = float(sig.get("totalTokenValue", 0))

                if total_value >= 50000:
                    buy_score = 10
                    buy_reason = f"聪明钱大额${total_value:,.0f}"
                elif sm_count >= 3:
                    buy_score = 6
                    buy_reason = f"聪明钱{sm_count}地址"
                else:
                    buy_score = 4
                    buy_reason = "聪明钱买入"

                score += buy_score
                reason = f"{buy_reason}+{buy_score}" + (f" {reason}" if reason else "")

            break
    except Exception as e:
        logger.debug(f"[smart_money_score] {ticker} 异常: {e}")

    if not reason:
        reason = "无聪明钱信号"

    return {"score": score, "reason": reason}


def get_news_score(symbol: str) -> dict:
    """兼容旧接口，汇总社交+聪明钱"""
    ss = get_social_score(symbol)
    sm = get_smart_money_score(symbol)
    total = ss["score"] + sm["score"]
    reasons = []
    if ss["score"] != 0:
        reasons.append(ss["reason"])
    if sm["score"] != 0:
        reasons.append(sm["reason"])
    return {
        "score": total,
        "reason": " | ".join(reasons) if reasons else "无匹配信号",
        "news_count": 1 if ss["score"] > 0 else 0,
        "whale_hits": 1 if sm["score"] > 0 else 0,
    }
