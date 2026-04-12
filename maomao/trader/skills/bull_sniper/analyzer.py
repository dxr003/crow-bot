#!/usr/bin/env python3
"""
bull_sniper analyzer.py — 信号分析器

两阶段触发：
  第一阶段：涨幅≥12% → 查新闻+查币安下架公告 → 有利好/下架反拉 → 直接推信号
  第二阶段：定期打分（无涨幅范围限制）→ 综合打分 ≥ 25分 → 推信号

评分体系 v2（瞬时爆发+趋势确认，2026-04-12）：
  瞬时爆发：1m>3%+5 / 3m>5%+8 / 5m>8%+10（可叠加）
  趋势确认：1h 5-10%+5 / 10-15%+8 / 15-25%+12 / 25-40%+15
  量比：1.5-2x+5 / 2-3x+10 / 3-5x+15 / >5x+18
  OI：上涨5-15%+5 / >15%+10 / 下跌-5
  挤空：多空比<0.8 +8
  费率：极端>±0.5% +8
  新闻：AI判断利好+5 / 利空-10（不再一票否决）

下架公告：独立通道，不走评分
手工开关：config.yaml → analyzer.enabled / 各阶段独立开关
"""
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Optional

import requests
import yaml
from pathlib import Path

BASE_DIR = Path(__file__).parent
logger = logging.getLogger("bull_analyzer")

# ── 币安公告缓存 ──
_delist_cache: dict = {"symbols": set(), "last_fetch": 0}
_DELIST_TTL = 3600  # 1小时刷新一次

# ── 新闻源失败计数（Tavily主力，Google RSS备用） ──
_tavily_fail_count = 0
_TAVILY_FAIL_THRESHOLD = 3  # 连续失败3次切换到Google RSS


def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", "r") as f:
        return yaml.safe_load(f)["bull_sniper"]


# ══════════════════════════════════════════
# 新闻查询
# ══════════════════════════════════════════

def fetch_news(symbol: str, cfg: dict) -> dict:
    """
    查询币种相关新闻
    优先级：Tavily → Google RSS → CoinGecko
    Tavily连续失败3次自动切换Google RSS，成功一次重置计数
    AI启用时：用Haiku判断情绪，失败回退关键词匹配
    返回: {"sentiment": "bullish"/"bearish"/"neutral", "level": "major"/"minor"/None, "titles": [...], "ai_reason": str|None}
    """
    global _tavily_fail_count
    news_cfg = cfg.get("news", {})
    base = symbol.replace("USDT", "").replace("BUSD", "")

    titles = []

    # Tavily主力
    if news_cfg.get("use_tavily", False) and _tavily_fail_count < _TAVILY_FAIL_THRESHOLD:
        tavily_titles = _fetch_tavily(base, news_cfg)
        if tavily_titles:
            _tavily_fail_count = 0
            titles += tavily_titles
        else:
            _tavily_fail_count += 1
            if _tavily_fail_count >= _TAVILY_FAIL_THRESHOLD:
                logger.warning(f"Tavily连续{_TAVILY_FAIL_THRESHOLD}次失败，切换Google RSS")

    # Google RSS备用（Tavily失败或没开启时）
    if not titles and news_cfg.get("use_google_rss", True):
        titles += _fetch_google_rss(base)

    # CoinGecko
    if not titles and news_cfg.get("use_coingecko", False):
        titles += _fetch_coingecko_news(base)

    if not titles:
        return {"sentiment": "neutral", "level": None, "titles": [], "ai_reason": None}

    # ── 位置1：AI新闻情绪判断（失败回退关键词） ──
    ai_cfg = _get_ai_config(cfg)
    if ai_cfg:
        ai_result = ai_news_sentiment(titles, symbol, ai_cfg)
        if ai_result:
            return {
                "sentiment": ai_result["sentiment"],
                "level": ai_result.get("level"),
                "titles": titles[:5],
                "ai_reason": ai_result.get("reason", ""),
            }
        # AI失败，回退关键词
        logger.info(f"AI新闻判断失败，回退关键词匹配 {symbol}")

    result = _classify_news(titles, news_cfg)
    result["ai_reason"] = None
    return result


def _fetch_tavily(base: str, news_cfg: dict) -> list:
    """Tavily Search API查询加密新闻"""
    try:
        api_key_env = news_cfg.get("tavily_api_key_env", "TAVILY_API_KEY")
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            logger.warning("TAVILY_API_KEY未配置")
            return []

        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": f"{base} cryptocurrency latest news",
                "search_depth": "basic",
                "max_results": 10,
                "include_answer": False,
                "include_domains": [
                    "coindesk.com", "cointelegraph.com", "theblock.co",
                    "decrypt.co", "blockworks.co", "cryptoslate.com",
                    "bitcoinmagazine.com", "coingecko.com",
                ],
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        titles = []
        for result in data.get("results", []):
            title = result.get("title", "")
            if title:
                titles.append(title.lower())
        logger.debug(f"Tavily {base}: {len(titles)}条")
        return titles
    except Exception as e:
        logger.warning(f"Tavily查询失败 {base}: {e}")
        return []


def _fetch_google_rss(base: str) -> list:
    """Google News RSS抓取（备用）"""
    try:
        url = (
            f"https://news.google.com/rss/search"
            f"?q={base}+crypto&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        )
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        titles = []
        for item in root.findall(".//item")[:10]:
            title_el = item.find("title")
            if title_el is not None and title_el.text:
                titles.append(title_el.text.lower())
        return titles
    except Exception as e:
        logger.warning(f"Google RSS获取失败: {e}")
        return []


def _fetch_coingecko_news(base: str) -> list:
    """CoinGecko新闻（需要API key）"""
    try:
        url = "https://api.coingecko.com/api/v3/news"
        resp = requests.get(url, timeout=8, params={"per_page": 20})
        resp.raise_for_status()
        items = resp.json()
        keyword = base.lower()
        return [
            item["title"].lower()
            for item in items
            if keyword in item.get("title", "").lower()
        ]
    except Exception as e:
        logger.warning(f"CoinGecko新闻获取失败: {e}")
        return []


def _classify_news(titles: list, news_cfg: dict) -> dict:
    """
    分类新闻情绪
    下架关键词不算利空，单独处理
    """
    delist_keywords = news_cfg.get("delist_keywords", [
        "delist", "delisting", "下架", "摘牌", "removed from"
    ])
    bullish_major = news_cfg.get("bullish_major_keywords", [
        "partnership", "合作", "listing", "上线", "mainnet", "主网",
        "upgrade", "升级", "etf", "adoption", "institutional", "机构",
        "acquisition", "收购", "airdrop", "空投"
    ])
    bullish_minor = news_cfg.get("bullish_minor_keywords", [
        "bullish", "pump", "surge", "rally", "gains", "up", "rise",
        "buy", "positive", "growth", "上涨", "利好", "突破"
    ])
    bearish_keywords = news_cfg.get("bearish_keywords", [
        "hack", "hacked", "exploit", "rug", "scam", "fraud", "exit scam",
        "sec", "lawsuit", "ban", "裁员", "黑客", "跑路", "欺诈", "监管",
        "shutdown", "bankruptcy", "破产"
    ])

    combined = " ".join(titles)

    # 过滤掉含下架关键词的标题，不参与利空判断
    non_delist_titles = [
        t for t in titles
        if not any(kw in t for kw in delist_keywords)
    ]
    non_delist_combined = " ".join(non_delist_titles)

    # 利空判断（排除下架相关）
    if any(kw in non_delist_combined for kw in bearish_keywords):
        return {"sentiment": "bearish", "level": None, "titles": titles[:5]}

    # 重大利好
    if any(kw in combined for kw in bullish_major):
        return {"sentiment": "bullish", "level": "major", "titles": titles[:5]}

    # 普通利好
    if any(kw in combined for kw in bullish_minor):
        return {"sentiment": "bullish", "level": "minor", "titles": titles[:5]}

    return {"sentiment": "neutral", "level": None, "titles": titles[:5]}


# ══════════════════════════════════════════
# BTC 基准数据
# ══════════════════════════════════════════

def get_btc_change_1h() -> float:
    """获取BTC 1小时涨幅"""
    try:
        resp = requests.get(
            f"{_FAPI_BASE}/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1h", "limit": 2},
            timeout=8,
        )
        resp.raise_for_status()
        klines = resp.json()
        prev = float(klines[-2][4])
        cur  = float(klines[-1][4])
        return round((cur - prev) / prev * 100, 2) if prev > 0 else 0.0
    except Exception as e:
        logger.debug(f"BTC涨幅获取失败: {e}")
        return 0.0


# ══════════════════════════════════════════
# AI 模块（Claude Haiku）
# ══════════════════════════════════════════

def _get_ai_config(cfg: dict) -> dict:
    """获取AI配置，返回空dict表示AI未启用"""
    ai_cfg = cfg.get("ai", {})
    if not ai_cfg.get("enabled", False):
        return {}
    api_key = os.getenv(ai_cfg.get("api_key_env", "ANTHROPIC_API_KEY"), "")
    if not api_key:
        logger.warning("AI已启用但API key未配置")
        return {}
    ai_cfg["_api_key"] = api_key
    return ai_cfg


def _call_haiku(prompt: str, ai_cfg: dict) -> Optional[dict]:
    """
    调用 Claude Haiku，返回解析后的JSON，失败返回None
    """
    api_key = ai_cfg.get("_api_key", "")
    model = ai_cfg.get("model", "claude-haiku-4-5-20251001")
    timeout = ai_cfg.get("timeout", 15)

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 256,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"].strip()
        # 提取JSON（可能被markdown包裹）
        json_match = re.search(r'\{[^{}]+\}', text)
        if json_match:
            return json.loads(json_match.group())
        return json.loads(text)
    except Exception as e:
        logger.warning(f"AI调用失败: {e}")
        return None


def ai_news_sentiment(titles: list, symbol: str, ai_cfg: dict) -> Optional[dict]:
    """
    位置1：AI判断新闻情绪
    返回: {"sentiment": "bullish"/"bearish"/"neutral", "reason": "..."}
    失败返回None，由调用方回退到关键词匹配
    """
    if not titles:
        return None
    if not ai_cfg.get("news_sentiment", False):
        return None

    base = symbol.replace("USDT", "").replace("BUSD", "")
    titles_text = "\n".join(f"- {t}" for t in titles[:8])

    prompt = (
        f"你是加密货币新闻分析师。以下是关于 {base} 的最新新闻标题：\n\n"
        f"{titles_text}\n\n"
        f"判断这些新闻对 {base} 价格的综合影响。\n"
        f"只返回JSON，格式：{{\"sentiment\": \"bullish\"/\"bearish\"/\"neutral\", "
        f"\"level\": \"major\"/\"minor\"/null, \"reason\": \"一句话理由\"}}\n"
        f"注意：下架/delist消息不算利空（可能反拉），归为neutral。"
    )

    result = _call_haiku(prompt, ai_cfg)
    if result and result.get("sentiment") in ("bullish", "bearish", "neutral"):
        logger.info(f"AI新闻判断 {symbol}: {result}")
        return result
    return None


def ai_final_decision(symbol: str, gain_pct: float, score: int,
                      breakdown: dict, market_data: dict,
                      news: dict, ai_cfg: dict) -> Optional[dict]:
    """
    位置2：AI最终决策
    返回: {"decision": "buy"/"skip", "reason": "..."}
    失败返回None → 按规则走（不阻止）
    """
    if not ai_cfg.get("final_decision", False):
        return None

    btc_1h = get_btc_change_1h()

    base = symbol.replace("USDT", "").replace("BUSD", "")
    news_summary = ", ".join(news.get("titles", [])[:3]) if news else "无新闻"
    breakdown_text = ", ".join(f"{k}={v}" for k, v in breakdown.items())

    system_prompt = (
        "你是加密货币做多交易决策AI，遵守以下规则：\n"
        "1. 综合评估涨幅、量比、OI、多空比、费率、新闻，判断是否值得做多\n"
        "2. 假突破迹象（量价背离、上影线过长）返回skip\n"
        "3. 明确利空新闻返回skip\n"
        "4. 资金费率极端拥挤（>0.05%）谨慎对待\n"
        "5. 若BTC同期1小时涨幅超过2%，且该币涨幅不超过BTC涨幅的3倍，"
        "判定为跟随行情非独立爆发，返回skip\n"
        "只返回JSON：{\"decision\": \"buy\"/\"skip\", \"reason\": \"一句话理由\"}"
    )

    user_prompt = (
        f"以下是 {base} 的实时数据：\n\n"
        f"涨幅: {gain_pct:.1f}%\n"
        f"综合评分: {score}分\n"
        f"评分明细: {breakdown_text}\n"
        f"OI变化: {market_data.get('oi_change_pct', 0):.1f}%\n"
        f"多空比: {market_data.get('long_short_ratio', 1.0):.2f}\n"
        f"资金费率: {market_data.get('funding_rate', 0)*100:.3f}%\n"
        f"量比: {market_data.get('volume_ratio', 1.0):.1f}x\n"
        f"BTC 1小时涨幅: {btc_1h:+.2f}%\n"
        f"新闻: {news_summary}\n\n"
        f"问题：现在做多 {base} 是否值得？"
    )

    # 用system+user双消息，比单prompt更精准
    api_key = ai_cfg.get("_api_key", "")
    model = ai_cfg.get("model", "claude-haiku-4-5-20251001")
    timeout = ai_cfg.get("timeout", 15)

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 256,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"].strip()
        json_match = re.search(r'\{[^{}]+\}', text)
        result = json.loads(json_match.group()) if json_match else json.loads(text)
    except Exception as e:
        logger.warning(f"AI决策调用失败: {e}")
        return None

    if result and result.get("decision") in ("buy", "skip"):
        logger.info(f"AI决策 {symbol}: {result} (BTC 1h: {btc_1h:+.2f}%)")
        return result
    return None


# ══════════════════════════════════════════
# 币安下架检测（原生API，铁律4）
# ══════════════════════════════════════════

_FAPI_BASE = "https://fapi.binance.com"

def fetch_delist_symbols(cfg: dict) -> set:
    """
    从币安 exchangeInfo 获取非 TRADING 状态的合约
    包括 SETTLING / PRE_DELIVERING / END_OF_DAY 等
    每小时缓存一次
    """
    global _delist_cache
    now = time.time()

    if now - _delist_cache["last_fetch"] < _DELIST_TTL:
        return _delist_cache["symbols"]

    try:
        resp = requests.get(f"{_FAPI_BASE}/fapi/v1/exchangeInfo", timeout=10)
        resp.raise_for_status()
        data = resp.json()

        symbols = set()
        for s in data.get("symbols", []):
            if not s["symbol"].endswith("USDT"):
                continue
            if s["status"] != "TRADING":
                base = s["symbol"].replace("USDT", "")
                symbols.add(base)

        _delist_cache = {"symbols": symbols, "last_fetch": now}
        logger.info(f"下架/非TRADING币更新: {len(symbols)}个")
        return symbols

    except Exception as e:
        logger.warning(f"exchangeInfo获取失败: {e}")
        return _delist_cache["symbols"]


def is_delist_target(symbol: str, cfg: dict) -> bool:
    """判断该币是否在下架/非TRADING状态"""
    base = symbol.replace("USDT", "").replace("BUSD", "")
    delist_symbols = fetch_delist_symbols(cfg)
    return base in delist_symbols


# ══════════════════════════════════════════
# 打分系统
# ══════════════════════════════════════════

def score_signal(symbol: str, gain_pct: float, market_data: dict, cfg: dict) -> dict:
    """
    综合打分 v2（瞬时爆发+趋势确认+量能+持仓+挤空+费率+新闻）
    market_data: {oi_change_pct, long_short_ratio, funding_rate, volume_ratio, change_1m, change_3m, change_5m}
    返回: {"score": int, "breakdown": dict, "vetoed": bool, "veto_reason": str}
    """
    scoring = cfg.get("scoring", {})
    breakdown = {}
    score = 0

    # ── 新闻评分（降权：利好+5，利空-10，不再一票否决） ──
    news_result = fetch_news(symbol, cfg)
    if news_result["sentiment"] == "bullish":
        pts = scoring.get("news_bullish", 5)
        breakdown["新闻利好"] = pts
        score += pts
    elif news_result["sentiment"] == "bearish":
        pts = scoring.get("news_bearish", -10)
        breakdown["新闻利空"] = pts
        score += pts

    # ── 瞬时爆发（1m/3m/5m可叠加） ──
    change_1m = market_data.get("change_1m", 0)
    change_3m = market_data.get("change_3m", 0)
    change_5m = market_data.get("change_5m", 0)

    if change_1m > 3:
        pts = scoring.get("burst_1m", 5)
        breakdown[f"1m爆发+{change_1m:.1f}%"] = pts
        score += pts
    if change_3m > 5:
        pts = scoring.get("burst_3m", 8)
        breakdown[f"3m爆发+{change_3m:.1f}%"] = pts
        score += pts
    if change_5m > 8:
        pts = scoring.get("burst_5m", 10)
        breakdown[f"5m爆发+{change_5m:.1f}%"] = pts
        score += pts

    # ── 趋势确认（1h涨幅分档） ──
    if 25 <= gain_pct < 40:
        pts = scoring.get("gain_25_40", 15)
        breakdown[f"1h趋势+{gain_pct:.1f}%"] = pts
        score += pts
    elif 15 <= gain_pct < 25:
        pts = scoring.get("gain_15_25", 12)
        breakdown[f"1h趋势+{gain_pct:.1f}%"] = pts
        score += pts
    elif 10 <= gain_pct < 15:
        pts = scoring.get("gain_10_15", 8)
        breakdown[f"1h趋势+{gain_pct:.1f}%"] = pts
        score += pts
    elif 5 <= gain_pct < 10:
        pts = scoring.get("gain_5_10", 5)
        breakdown[f"1h趋势+{gain_pct:.1f}%"] = pts
        score += pts

    # ── OI变化（分档） ──
    oi_change = market_data.get("oi_change_pct", 0)
    if oi_change > 15:
        pts = scoring.get("oi_up_strong", 10)
        breakdown[f"OI大涨+{oi_change:.1f}%"] = pts
        score += pts
    elif oi_change > 5:
        pts = scoring.get("oi_up_mild", 5)
        breakdown[f"OI上涨+{oi_change:.1f}%"] = pts
        score += pts
    elif oi_change < -5:
        pts = scoring.get("oi_down", -5)
        breakdown[f"OI下跌{oi_change:.1f}%"] = pts
        score += pts

    # ── 多空比 ──
    lsr = market_data.get("long_short_ratio", 1.0)
    if lsr < 0.8:
        pts = scoring.get("lsr_short_dominant", 8)
        breakdown[f"挤空{lsr:.2f}"] = pts
        score += pts

    # ── 资金费率 ──
    funding = market_data.get("funding_rate", 0)
    funding_threshold = scoring.get("funding_extreme_threshold", 0.005)
    if abs(funding) >= funding_threshold:
        pts = scoring.get("funding_extreme", 8)
        direction = "负" if funding < 0 else "正"
        breakdown[f"费率极端{direction}{funding*100:.3f}%"] = pts
        score += pts

    # ── 量比（多一档>5x） ──
    volume_ratio = market_data.get("volume_ratio", 1.0)
    if volume_ratio > 5:
        pts = scoring.get("vol_ratio_5x", 18)
        breakdown[f"量比{volume_ratio:.1f}x"] = pts
        score += pts
    elif volume_ratio > 3:
        pts = scoring.get("vol_ratio_3x", 15)
        breakdown[f"量比{volume_ratio:.1f}x"] = pts
        score += pts
    elif volume_ratio > 2:
        pts = scoring.get("vol_ratio_2x", 10)
        breakdown[f"量比{volume_ratio:.1f}x"] = pts
        score += pts
    elif volume_ratio > 1.5:
        pts = scoring.get("vol_ratio_1_5x", 5)
        breakdown[f"量比{volume_ratio:.1f}x"] = pts
        score += pts
    elif volume_ratio < 1:
        pts = scoring.get("vol_ratio_low", -5)
        breakdown[f"量比{volume_ratio:.1f}x萎缩"] = pts
        score += pts

    return {
        "score": score, "breakdown": breakdown,
        "vetoed": False, "veto_reason": "", "news": news_result
    }


# ══════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════

def analyze(symbol: str, gain_pct: float, market_data: dict, cfg: Optional[dict] = None) -> dict:
    """
    主分析入口
    返回:
      {"action": "signal_fast", "reason": "利好新闻"}   → 第一阶段快速触发
      {"action": "signal_fast", "reason": "下架反拉"}   → 下架通道触发
      {"action": "signal_scored", "score": 45, ...}     → 第二阶段打分触发
      {"action": "hold", ...}                            → 继续观察
      {"action": "veto", "reason": "利空新闻否决"}       → 否决
    """
    if cfg is None:
        cfg = load_config()

    analyzer_cfg = cfg.get("analyzer", {})

    # 手工总开关
    if not analyzer_cfg.get("enabled", True):
        return {"action": "hold", "reason": "analyzer已关闭"}

    # ── 第一阶段：8%快速通道 ──
    stage1_threshold = analyzer_cfg.get("stage1_gain_pct", 8)
    if gain_pct >= stage1_threshold and analyzer_cfg.get("stage1_enabled", True):

        # 下架反拉（独立通道，不受新闻否决影响）
        if analyzer_cfg.get("delist_enabled", True) and is_delist_target(symbol, cfg):
            return {
                "action": "signal_fast",
                "reason": "下架反拉",
                "gain_pct": gain_pct,
                "score": None,
            }

        # 新闻快速通道（利空不再否决，只有利好才快速触发）
        news = fetch_news(symbol, cfg)
        if news["sentiment"] == "bullish":
            return {
                "action": "signal_fast",
                "reason": f"利好新闻({news['level']})",
                "gain_pct": gain_pct,
                "news": news,
                "score": None,
            }

    # ── 第二阶段：打分通道（无涨幅范围限制，由评分体系自行判断） ──
    signal_threshold = analyzer_cfg.get("signal_threshold", 25)

    if analyzer_cfg.get("stage2_enabled", True):
        result = score_signal(symbol, gain_pct, market_data, cfg)

        if result["score"] >= signal_threshold:
            # ── 位置2：AI最终决策（skip否决，buy/失败放行） ──
            ai_cfg = _get_ai_config(cfg)
            ai_decision = None
            if ai_cfg:
                ai_decision = ai_final_decision(
                    symbol, gain_pct, result["score"],
                    result["breakdown"], market_data,
                    result.get("news", {}), ai_cfg
                )
                if ai_decision and ai_decision["decision"] == "skip":
                    return {
                        "action": "veto",
                        "reason": f"AI否决: {ai_decision.get('reason', '')}",
                        "score": result["score"],
                        "breakdown": result["breakdown"],
                        "news": result.get("news"),
                        "ai_decision": ai_decision,
                    }

            return {
                "action": "signal_scored",
                "score": result["score"],
                "breakdown": result["breakdown"],
                "gain_pct": gain_pct,
                "news": result.get("news"),
                "ai_decision": ai_decision,
            }
        else:
            return {
                "action": "hold",
                "reason": f"评分{result['score']}分未达{signal_threshold}分",
                "score": result["score"],
                "breakdown": result["breakdown"],
            }

    return {"action": "hold", "reason": f"1h+{gain_pct:.1f}%，等待评分达标"}
