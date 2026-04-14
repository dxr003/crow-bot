#!/usr/bin/env python3
"""
bull_sniper analyzer.py — 信号分析器 v3.1

统一评分体系，无快速通道：
  7因子：A动能 + B趋势 + C量比 + D-OI费率 + E社交聪明钱 + F链上 + G公告
  ≥38分 → AI最终决策 → 推信号
  理论满分116

2026-04-13 v3.1 重构
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

# ── 新闻源（Google RSS免费方案） ──


def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", "r") as f:
        return yaml.safe_load(f)["bull_sniper"]


# ══════════════════════════════════════════
# 新闻查询
# ══════════════════════════════════════════

def fetch_news(symbol: str, cfg: dict) -> dict:
    """
    查询币种相关新闻
    优先级：Google RSS → CoinGecko
    AI启用时：用Haiku判断情绪，失败回退关键词匹配
    返回: {"sentiment": "bullish"/"bearish"/"neutral", "level": "major"/"minor"/None, "titles": [...], "ai_reason": str|None}
    """
    news_cfg = cfg.get("news", {})
    base = symbol.replace("USDT", "").replace("BUSD", "")

    titles = []

    # Google RSS（免费，通用搜索）
    if news_cfg.get("use_google_rss", True):
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
        f"你是加密货币新闻分析师。以下是搜索 {base} 得到的新闻标题：\n\n"
        f"{titles_text}\n\n"
        f"判断这些新闻对 {base} 价格的综合影响。\n"
        f"核心规则：只有直接提到 {base} 项目本身的新闻才算数。"
        f"泛市场新闻（如BTC走势、交易所政策、宏观经济）即使搜索结果里出现了，"
        f"只要不是专门针对 {base} 的，一律视为neutral，不加分不减分。\n"
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
        f"你是加密货币做多交易决策AI，当前目标币种：{base}。遵守以下规则：\n"
        "1. 综合评估涨幅、量比、OI、多空比、费率、新闻，判断是否值得做多\n"
        "2. 假突破迹象（量价背离、上影线过长）返回skip\n"
        f"3. 只有直接针对 {base} 项目本身的利空新闻才能作为skip依据。"
        "泛市场新闻（BTC走势、交易所政策、宏观经济等）与该币无直接关系，不作为判断依据\n"
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
    综合打分 v3.1（7因子：动能+趋势+量比+OI费率+社交聪明钱+链上+公告）
    A-C互斥取最高，D独立叠加，E社交/聪明钱各自互斥，F叠加规则见文档，G独立
    """
    scoring = cfg.get("scoring", {})
    breakdown = {}
    score = 0

    # ── A. 价格动能（互斥取最高） ──
    change_1m = market_data.get("change_1m", 0)
    change_3m = market_data.get("change_3m", 0)
    change_5m = market_data.get("change_5m", 0)

    burst_1m_th = scoring.get("burst_1m_threshold", 12)
    burst_3m_th = scoring.get("burst_3m_threshold", 8)
    burst_5m_th = scoring.get("burst_5m_threshold", 7)

    if change_1m > burst_1m_th:
        pts = scoring.get("burst_1m", 8)
        breakdown[f"A.1m爆发+{change_1m:.1f}%"] = pts
        score += pts
    elif change_3m > burst_3m_th:
        pts = scoring.get("burst_3m", 7)
        breakdown[f"A.3m爆发+{change_3m:.1f}%"] = pts
        score += pts
    elif change_5m > burst_5m_th:
        pts = scoring.get("burst_5m", 6)
        breakdown[f"A.5m爆发+{change_5m:.1f}%"] = pts
        score += pts

    # ── B. 池内涨幅（互斥取最高，越早越高） ──
    if 5 <= gain_pct < 10:
        pts = scoring.get("gain_5_10", 10)
        breakdown[f"B.涨幅初期+{gain_pct:.1f}%"] = pts
        score += pts
    elif 10 <= gain_pct < 15:
        pts = scoring.get("gain_10_15", 8)
        breakdown[f"B.涨幅中期+{gain_pct:.1f}%"] = pts
        score += pts
    elif 15 <= gain_pct < 25:
        pts = scoring.get("gain_15_25", 7)
        breakdown[f"B.涨幅强势+{gain_pct:.1f}%"] = pts
        score += pts
    elif 25 <= gain_pct < 40:
        pts = scoring.get("gain_25_40", 5)
        breakdown[f"B.涨幅过热+{gain_pct:.1f}%"] = pts
        score += pts

    # ── C. 量比（互斥取最高） ──
    volume_ratio = market_data.get("volume_ratio", 1.0)
    if volume_ratio > 5:
        pts = scoring.get("vol_ratio_5x", 18)
        breakdown[f"C.量比{volume_ratio:.1f}x"] = pts
        score += pts
    elif volume_ratio > 3:
        pts = scoring.get("vol_ratio_3x", 15)
        breakdown[f"C.量比{volume_ratio:.1f}x"] = pts
        score += pts
    elif volume_ratio > 2:
        pts = scoring.get("vol_ratio_2x", 10)
        breakdown[f"C.量比{volume_ratio:.1f}x"] = pts
        score += pts
    elif volume_ratio > 1.5:
        pts = scoring.get("vol_ratio_1_5x", 5)
        breakdown[f"C.量比{volume_ratio:.1f}x"] = pts
        score += pts
    elif volume_ratio < 1:
        pts = scoring.get("vol_ratio_low", -5)
        breakdown[f"C.量比{volume_ratio:.1f}x萎缩"] = pts
        score += pts

    # ── D. OI与资金费率（独立可叠加） ──
    oi_change = market_data.get("oi_change_pct", 0)
    if oi_change > 30:
        pts = scoring.get("oi_up_super", 10)
        breakdown[f"D.OI超涨+{oi_change:.1f}%"] = pts
        score += pts
    elif oi_change > 15:
        pts = scoring.get("oi_up_strong", 8)
        breakdown[f"D.OI大涨+{oi_change:.1f}%"] = pts
        score += pts
    elif oi_change > 5:
        pts = scoring.get("oi_up_mild", 5)
        breakdown[f"D.OI上涨+{oi_change:.1f}%"] = pts
        score += pts
    elif oi_change < -5:
        pts = scoring.get("oi_down", -5)
        breakdown[f"D.OI下跌{oi_change:.1f}%"] = pts
        score += pts

    lsr = market_data.get("long_short_ratio", 1.0)
    if lsr < 0.8:
        pts = scoring.get("lsr_short_dominant", 2)
        breakdown[f"D.挤空{lsr:.2f}"] = pts
        score += pts

    funding = market_data.get("funding_rate", 0)
    funding_threshold = scoring.get("funding_extreme_threshold", 0.0005)
    if abs(funding) >= funding_threshold:
        pts = scoring.get("funding_extreme", 3)
        direction = "负" if funding < 0 else "正"
        breakdown[f"D.费率{direction}{funding*100:.3f}%"] = pts
        score += pts

    # ── E. 捉妖因子（链上数据，含否决） ──
    try:
        from chain_score import get_chain_score
        cs = get_chain_score(symbol, cfg)
        if cs.get("vetoed"):
            return {
                "score": score, "breakdown": breakdown,
                "vetoed": True, "veto_reason": f"E因子否决:{cs['veto_reason']}",
            }
        if cs["score"] != 0:
            breakdown[f"E.{cs['reason']}"] = cs["score"]
            score += cs["score"]
    except Exception as e:
        logger.warning(f"[E因子] {symbol} 跳过: {e}")

    # ── G. 公告因子（读缓存） ──
    try:
        announce = market_data.get("announce_status", "")
        if announce == "new_listing":
            pts = scoring.get("announce_new_listing", 5)
            breakdown["G.上新交易所"] = pts
            score += pts
        elif announce == "delist":
            pts = scoring.get("announce_delist", -5)
            breakdown["G.下架公告"] = pts
            score += pts
    except Exception as e:
        logger.warning(f"[G因子] {symbol} 跳过: {e}")

    return {
        "score": score, "breakdown": breakdown,
        "vetoed": False, "veto_reason": "",
    }


# ══════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════

def analyze(symbol: str, gain_pct: float, market_data: dict, cfg: Optional[dict] = None) -> dict:
    """
    主分析入口 v3.1 — 统一打分，无快速通道
    返回:
      {"action": "signal_scored", "score": 45, ...}     → 打分达标
      {"action": "hold", ...}                            → 继续观察
      {"action": "veto", "reason": "AI否决: ..."}        → 否决
    """
    if cfg is None:
        cfg = load_config()

    analyzer_cfg = cfg.get("analyzer", {})

    if not analyzer_cfg.get("enabled", True):
        return {"action": "hold", "reason": "analyzer已关闭"}

    signal_threshold = analyzer_cfg.get("signal_threshold", 38)

    result = score_signal(symbol, gain_pct, market_data, cfg)

    if result.get("vetoed"):
        return {
            "action": "hold",
            "reason": result.get("veto_reason", "E因子否决"),
            "score": result["score"],
            "breakdown": result["breakdown"],
        }

    if result["score"] >= signal_threshold:
        ai_cfg = _get_ai_config(cfg)
        ai_decision = None
        if ai_cfg:
            ai_decision = ai_final_decision(
                symbol, gain_pct, result["score"],
                result["breakdown"], market_data,
                {}, ai_cfg
            )
            if ai_decision and ai_decision["decision"] == "skip":
                return {
                    "action": "veto",
                    "reason": f"AI否决: {ai_decision.get('reason', '')}",
                    "score": result["score"],
                    "breakdown": result["breakdown"],
                    "ai_decision": ai_decision,
                }

        return {
            "action": "signal_scored",
            "score": result["score"],
            "breakdown": result["breakdown"],
            "gain_pct": gain_pct,
            "ai_decision": ai_decision,
        }

    return {
        "action": "hold",
        "reason": f"评分{result['score']}分未达{signal_threshold}分",
        "score": result["score"],
        "breakdown": result["breakdown"],
    }
