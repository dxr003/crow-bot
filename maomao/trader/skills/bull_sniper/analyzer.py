#!/usr/bin/env python3
"""
bull_sniper analyzer.py — 信号分析器 v3.5-minimalist

纯规则评分，无 AI / 新闻 / 链上：
  4因子：DN动能 + WL位置 + TP突破 + DD量能
  满分 105；信号阈值 60 分
  硬触发：AF Alpha+2 / GG 公告+3或-5
"""
import logging
import time
from typing import Optional

import requests
import yaml
from pathlib import Path

BASE_DIR = Path(__file__).parent
logger = logging.getLogger("bull_analyzer")

# ── 币安公告缓存 ──
_delist_cache: dict = {"symbols": set(), "last_fetch": 0}
_DELIST_TTL = 3600


def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", "r") as f:
        return yaml.safe_load(f)["bull_sniper"]


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
    综合打分 v3.5-minimalist（4因子：DN动能 + WL位置 + TP突破 + DD量能）
    满分 105：DN55 + WL15 + TP20 + DD15；信号阈值 60 分
    硬触发：AF Alpha+2 / GG 公告+3或-5
    """
    scoring = cfg.get("scoring", {})
    breakdown = {}
    score = 0

    # ── DN. 动能涨幅（v3.5 互斥取最高，阈值 7/5/4/8，分值 55/42/33/22） ──
    change_1m  = market_data.get("change_1m",  0)
    change_3m  = market_data.get("change_3m",  0)
    change_5m  = market_data.get("change_5m",  0)
    change_15m = market_data.get("change_15m", 0)
    change_1h  = market_data.get("change_1h",  0)

    dn_pts = 0
    dn_reverse = scoring.get("dn_reverse_guard", -1)

    # ── 趋势硬否决（2026-04-26 老大授权加，AGT/BSB 两单连损根因）──
    # 1h 趋势 < 0 = 跌势中反弹，DN 因子全部归零（信号自然不过线）
    trend_reject_th = scoring.get("trend_reject_1h_pct", 0)
    if change_1h < trend_reject_th:
        breakdown[f"DN.1h趋势否决({change_1h:+.1f}%)"] = 0
        # dn_pts 保持 0，跳过 DN 计算
    elif change_1m < dn_reverse:
        # 反转守门：1m 已在回撤，DN 归零
        breakdown["DN.反转守门"] = 0
    else:
        dn_1m_th  = scoring.get("dn_burst_1m",  7)
        dn_3m_th  = scoring.get("dn_burst_3m",  5)
        dn_5m_th  = scoring.get("dn_burst_5m",  4)
        dn_15m_th = scoring.get("dn_burst_15m", 8)

        if change_1m > dn_1m_th:
            dn_pts = scoring.get("dn_score_1m", 55)
            breakdown[f"DN.1m爆发+{change_1m:.1f}%"] = dn_pts
        elif change_3m > dn_3m_th:
            dn_pts = scoring.get("dn_score_3m", 42)
            breakdown[f"DN.3m爆发+{change_3m:.1f}%"] = dn_pts
        elif change_5m > dn_5m_th:
            dn_pts = scoring.get("dn_score_5m", 33)
            breakdown[f"DN.5m爆发+{change_5m:.1f}%"] = dn_pts
        elif change_15m > dn_15m_th:
            dn_pts = scoring.get("dn_score_15m", 22)
            breakdown[f"DN.15m爆发+{change_15m:.1f}%"] = dn_pts

        # ── 弱趋势降权（1h+0~5%，可能横盘抢反弹）──
        weak_trend_th = scoring.get("trend_weak_1h_pct", 5)
        if dn_pts > 0 and change_1h < weak_trend_th:
            old = dn_pts
            dn_pts = int(dn_pts * 0.5)
            # 替换最近一条 DN.* breakdown 标签
            for k in list(breakdown.keys()):
                if k.startswith("DN.") and breakdown[k] == old:
                    breakdown[f"{k} 1h弱趋势×0.5"] = dn_pts
                    del breakdown[k]
                    break

        # ── 山顶守门（1m ≥ 10% 视为顶部接盘点，DN 砍半）──
        peak_1m_th = scoring.get("peak_1m_pct", 10)
        if dn_pts > 0 and change_1m >= peak_1m_th:
            old = dn_pts
            dn_pts = int(dn_pts * 0.5)
            for k in list(breakdown.keys()):
                if k.startswith("DN.") and breakdown[k] == old:
                    breakdown[f"{k} 1m山顶×0.5"] = dn_pts
                    del breakdown[k]
                    break

    score += dn_pts

    # ── WL. 位置（v3.5 互斥取最高，基准=entry_price_in_pool，4档: 15/12/8/3） ──
    wl_pts = 0
    if 5 <= gain_pct < 10:
        wl_pts = scoring.get("wl_score_tier1", 15)
        breakdown[f"WL.早期+{gain_pct:.1f}%"] = wl_pts
    elif 10 <= gain_pct < 15:
        wl_pts = scoring.get("wl_score_tier2", 12)
        breakdown[f"WL.中期+{gain_pct:.1f}%"] = wl_pts
    elif 15 <= gain_pct < 25:
        wl_pts = scoring.get("wl_score_tier3", 8)
        breakdown[f"WL.强势+{gain_pct:.1f}%"] = wl_pts
    elif 25 <= gain_pct < 40:
        wl_pts = scoring.get("wl_score_tier4", 3)
        breakdown[f"WL.过热+{gain_pct:.1f}%"] = wl_pts
    # <5% 或 ≥40% → 0（太早/过热）
    score += wl_pts

    # ── TP. 突破因子（v3.5，1H 收盘突破前高 + 量 + 上影过滤） ──
    try:
        from tp_score import score_tp
        tp_pts, tp_reason = score_tp(symbol, cfg)
        if tp_pts > 0:
            breakdown[f"TP.{tp_reason}"] = tp_pts
            score += tp_pts
    except Exception as e:
        logger.warning(f"[TP层] {symbol} 跳过: {e}")

    # ── DD. 有方向的量能（v3.5，aggTrades 5 分钟 taker 买卖比） ──
    try:
        from dd_score import score_dd
        dd_pts, dd_reason = score_dd(symbol, cfg)
        if dd_pts > 0:
            breakdown[f"DD.{dd_reason}"] = dd_pts
            score += dd_pts
    except Exception as e:
        logger.warning(f"[DD层] {symbol} 跳过: {e}")

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

    # ── AF. 量大涨大加权（2026-04-25 17:00 老大补加，覆盖漏网 KAT/SOON 等）──
    # 量 ≥ 1亿U → +10；24h 涨幅 ≥ 25% → +10；双满最多 +20
    try:
        af_chg24 = float(market_data.get("change_24h", 0))
        af_vol24 = float(market_data.get("volume_24h_usdt", 0))
        af_pts = 0
        af_reasons = []
        if af_vol24 >= scoring.get("af_volume_threshold", 100_000_000):
            af_pts += scoring.get("af_volume_score", 10)
            af_reasons.append(f"量{af_vol24/1e8:.1f}亿")
        if af_chg24 >= scoring.get("af_change_threshold", 25):
            af_pts += scoring.get("af_change_score", 10)
            af_reasons.append(f"24h+{af_chg24:.0f}%")
        if af_pts > 0:
            breakdown[f"AF.加权({'/'.join(af_reasons)})"] = af_pts
            score += af_pts
    except Exception as e:
        logger.warning(f"[AF加权] {symbol} 跳过: {e}")

    return {"score": score, "breakdown": breakdown}


# ══════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════

def analyze(symbol: str, gain_pct: float, market_data: dict, cfg: Optional[dict] = None) -> dict:
    """
    主分析入口 v3.5 — 纯规则打分，无 AI / 新闻 / 链上
    返回:
      {"action": "signal_scored", "score": N, ...}  → 达到阈值
      {"action": "hold", ...}                        → 未达阈值
    """
    if cfg is None:
        cfg = load_config()

    analyzer_cfg = cfg.get("analyzer", {})

    if not analyzer_cfg.get("enabled", True):
        return {"action": "hold", "reason": "analyzer已关闭"}

    signal_threshold = analyzer_cfg.get("signal_threshold", 60)

    result = score_signal(symbol, gain_pct, market_data, cfg)

    if result["score"] >= signal_threshold:
        return {
            "action": "signal_scored",
            "score": result["score"],
            "breakdown": result["breakdown"],
            "gain_pct": gain_pct,
        }

    return {
        "action": "hold",
        "reason": f"评分{result['score']}分未达{signal_threshold}分",
        "score": result["score"],
        "breakdown": result["breakdown"],
    }
