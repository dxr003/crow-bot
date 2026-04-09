#!/usr/bin/env python3
"""
bull_sniper analyzer.py — 信号分析器

两阶段触发：
  第一阶段：涨幅≥8% → 查新闻+查币安下架公告 → 有利好/下架反拉 → 直接推信号
  第二阶段：涨幅10-20% → 综合打分 ≥ 阈值 → 推信号

评分体系（全部参数在config.yaml可调）：
  新闻：重大利好+15 / 普通利好+5 / 利空直接否决
  涨幅：10-15%+10 / 15-20%+20
  OI：上涨+10 / 下跌-5
  多空比：<0.8空头占多+10 / >1.5多头过热-5
  费率：≤-0.5%或≥+0.5%+10
  量比：>3倍+15 / 2-3倍+10 / 1.5-2倍+5 / <1倍-5

下架公告：独立通道，不走评分，不受新闻利空否决影响
手工开关：config.yaml → analyzer.enabled / 各阶段独立开关
"""
import logging
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


def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", "r") as f:
        return yaml.safe_load(f)["bull_sniper"]


# ══════════════════════════════════════════
# 新闻查询
# ══════════════════════════════════════════

def fetch_news(symbol: str, cfg: dict) -> dict:
    """
    查询币种相关新闻
    返回: {"sentiment": "bullish"/"bearish"/"neutral", "level": "major"/"minor"/None, "titles": [...]}
    """
    news_cfg = cfg.get("news", {})
    base = symbol.replace("USDT", "").replace("BUSD", "")

    titles = []

    # Google News RSS
    if news_cfg.get("use_google_rss", True):
        titles += _fetch_google_rss(base)

    # CoinGecko
    if news_cfg.get("use_coingecko", False):
        titles += _fetch_coingecko_news(base)

    if not titles:
        return {"sentiment": "neutral", "level": None, "titles": []}

    return _classify_news(titles, news_cfg)


def _fetch_google_rss(base: str) -> list:
    """Google News RSS抓取"""
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
# 币安下架公告
# ══════════════════════════════════════════

def fetch_delist_symbols(cfg: dict) -> set:
    """
    拉取币安下架公告，返回涉及的币种集合
    每小时缓存一次
    """
    global _delist_cache
    now = time.time()

    if now - _delist_cache["last_fetch"] < _DELIST_TTL:
        return _delist_cache["symbols"]

    try:
        url = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
        params = {
            "type": 1,
            "catalogId": 161,  # 下架公告分类
            "pageNo": 1,
            "pageSize": 20,
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        symbols = set()
        articles = data.get("data", {}).get("articles", [])
        for article in articles:
            title = article.get("title", "")
            found = re.findall(r'\b([A-Z]{2,10})\b', title)
            for sym in found:
                if sym not in {"USDT", "BUSD", "USD", "BTC", "ETH", "AND", "FOR", "THE"}:
                    symbols.add(sym)

        _delist_cache = {"symbols": symbols, "last_fetch": now}
        logger.info(f"币安下架公告更新: {symbols}")
        return symbols

    except Exception as e:
        logger.warning(f"币安下架公告获取失败: {e}")
        return _delist_cache["symbols"]


def is_delist_target(symbol: str, cfg: dict) -> bool:
    """判断该币是否在下架名单中"""
    base = symbol.replace("USDT", "").replace("BUSD", "")
    delist_symbols = fetch_delist_symbols(cfg)
    return base in delist_symbols


# ══════════════════════════════════════════
# 打分系统
# ══════════════════════════════════════════

def score_signal(symbol: str, gain_pct: float, market_data: dict, cfg: dict) -> dict:
    """
    综合打分
    market_data: {oi_change_pct, long_short_ratio, funding_rate, volume_ratio}
    返回: {"score": int, "breakdown": dict, "vetoed": bool, "veto_reason": str}
    """
    scoring = cfg.get("scoring", {})
    breakdown = {}
    score = 0

    # ── 新闻评分 ──
    news_result = fetch_news(symbol, cfg)
    if news_result["sentiment"] == "bearish":
        return {
            "score": 0, "breakdown": {}, "vetoed": True,
            "veto_reason": "利空新闻否决", "news": news_result
        }

    if news_result["level"] == "major":
        pts = scoring.get("news_major", 15)
        breakdown["新闻重大利好"] = pts
        score += pts
    elif news_result["level"] == "minor":
        pts = scoring.get("news_minor", 5)
        breakdown["新闻普通利好"] = pts
        score += pts

    # ── 涨幅评分 ──
    if 15 <= gain_pct < 20:
        pts = scoring.get("gain_15_20", 20)
        breakdown[f"涨幅{gain_pct:.1f}%"] = pts
        score += pts
    elif 10 <= gain_pct < 15:
        pts = scoring.get("gain_10_15", 10)
        breakdown[f"涨幅{gain_pct:.1f}%"] = pts
        score += pts

    # ── OI变化 ──
    oi_change = market_data.get("oi_change_pct", 0)
    if oi_change > 0:
        pts = scoring.get("oi_up", 10)
        breakdown[f"OI上涨{oi_change:.1f}%"] = pts
        score += pts
    elif oi_change < -5:
        pts = scoring.get("oi_down", -5)
        breakdown[f"OI下跌{oi_change:.1f}%"] = pts
        score += pts

    # ── 多空比 ──
    lsr = market_data.get("long_short_ratio", 1.0)
    if lsr < 0.8:
        pts = scoring.get("lsr_short_dominant", 10)
        breakdown[f"多空比{lsr:.2f}空头占多"] = pts
        score += pts
    elif lsr > 1.5:
        pts = scoring.get("lsr_long_crowded", -5)
        breakdown[f"多空比{lsr:.2f}多头过热"] = pts
        score += pts

    # ── 资金费率 ──
    funding = market_data.get("funding_rate", 0)
    funding_threshold = scoring.get("funding_extreme_threshold", 0.005)
    if abs(funding) >= funding_threshold:
        pts = scoring.get("funding_extreme", 10)
        direction = "负" if funding < 0 else "正"
        breakdown[f"费率极端{direction}{funding*100:.3f}%"] = pts
        score += pts

    # ── 量比 ──
    volume_ratio = market_data.get("volume_ratio", 1.0)
    if volume_ratio > 3:
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

        # 新闻快速通道
        news = fetch_news(symbol, cfg)
        if news["sentiment"] == "bearish":
            return {"action": "veto", "reason": "利空新闻否决", "news": news}
        if news["sentiment"] == "bullish":
            return {
                "action": "signal_fast",
                "reason": f"利好新闻({news['level']})",
                "gain_pct": gain_pct,
                "news": news,
                "score": None,
            }

    # ── 第二阶段：10-20%打分通道 ──
    stage2_min = analyzer_cfg.get("stage2_gain_min", 10)
    stage2_max = analyzer_cfg.get("stage2_gain_max", 20)
    signal_threshold = analyzer_cfg.get("signal_threshold", 30)

    if stage2_min <= gain_pct <= stage2_max and analyzer_cfg.get("stage2_enabled", True):
        result = score_signal(symbol, gain_pct, market_data, cfg)

        if result["vetoed"]:
            return {"action": "veto", "reason": result["veto_reason"], "news": result.get("news")}

        if result["score"] >= signal_threshold:
            return {
                "action": "signal_scored",
                "score": result["score"],
                "breakdown": result["breakdown"],
                "gain_pct": gain_pct,
                "news": result.get("news"),
            }
        else:
            return {
                "action": "hold",
                "reason": f"评分{result['score']}分未达{signal_threshold}分",
                "score": result["score"],
                "breakdown": result["breakdown"],
            }

    return {"action": "hold", "reason": f"涨幅{gain_pct:.1f}%未达触发条件"}
