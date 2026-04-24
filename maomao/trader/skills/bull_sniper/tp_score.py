"""
TP 层：1H 收盘突破评分 v3.5-minimalist
前置门：收盘突破 / 首次突破 / 上影<40%
档位：vol 2.5x+盘整=20 / 2.0x=15 / 1.5x=10 / <1.5x=5（弱突破）
"""
import time
import logging
import requests

logger = logging.getLogger("bull_sniper.tp_score")

FAPI_BASE = "https://fapi.binance.com"

_kline_cache: dict = {}  # {symbol: (klines, expire_ts)}


def _fetch_klines(symbol: str, limit: int, ttl_sec: int) -> list:
    key = symbol
    now = time.time()
    hit = _kline_cache.get(key)
    if hit and now < hit[1]:
        return hit[0]
    resp = requests.get(
        f"{FAPI_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": "1h", "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    klines = resp.json()
    _kline_cache[key] = (klines, now + ttl_sec)
    return klines


def score_tp(symbol: str, cfg: dict) -> tuple[int, str]:
    """
    TP 因子主入口，返回 (score, reason)。
    fail-open: API 失败 / 数据不足 → (0, "")
    """
    scoring    = cfg.get("scoring", {})
    lookback_n = scoring.get("tp_lookback_1h", 6)
    margin     = scoring.get("tp_breakout_margin", 0.003)
    wick_max   = scoring.get("tp_wick_ratio_max", 0.40)
    consol_pct = scoring.get("tp_consolidation_range", 0.08)
    cache_sec  = scoring.get("tp_cache_minutes", 60) * 60

    try:
        klines = _fetch_klines(symbol, limit=lookback_n + 2, ttl_sec=cache_sec)
        if len(klines) < lookback_n + 1:
            return 0, ""  # fail-open：数据不足

        current  = klines[-1]
        prev     = klines[-2]
        lookback = klines[-(lookback_n + 1):-1]

        cur_close  = float(current[4])
        cur_high   = float(current[2])
        cur_low    = float(current[3])
        cur_vol    = float(current[5])
        prev_close = float(prev[4])

        lb_closes = [float(k[4]) for k in lookback]
        lb_vols   = [float(k[5]) for k in lookback]

        # 前高用 close（不用 high，避免影线污染）
        prev_high_close    = max(lb_closes)
        breakout_threshold = prev_high_close * (1 + margin)

        # 前置门 1：收盘突破
        if cur_close < breakout_threshold:
            return 0, ""

        # 前置门 4：首次突破（前一根未突破）
        if prev_close >= prev_high_close:
            return 0, ""

        # 前置门 3：上影过滤
        wick_ratio = (cur_high - cur_close) / (cur_high - cur_low + 1e-9)
        if wick_ratio >= wick_max:
            return 0, ""

        # 量比（不作硬门，决定档位；vol<1.5x → 弱突破=5，见 tp_weak_score 注释）
        avg_vol   = sum(lb_vols) / len(lb_vols) if lb_vols else 1e-9
        vol_ratio = cur_vol / (avg_vol + 1e-9)

        # 盘整质量：前 N 根收盘价波动幅度 < 8%
        lo = min(lb_closes)
        hi = max(lb_closes)
        consol_good = (hi / (lo + 1e-9) - 1) < consol_pct

        # 档位判定（互斥取最高）
        if vol_ratio >= 2.5 and consol_good:
            return scoring.get("tp_super_score", 20), f"TP超强突破 量{vol_ratio:.1f}x+盘整"
        if vol_ratio >= 2.0:
            return scoring.get("tp_strong_score", 15), f"TP强突破 量{vol_ratio:.1f}x"
        if vol_ratio >= 1.5:
            return scoring.get("tp_normal_score", 10), f"TP突破 量{vol_ratio:.1f}x"
        return scoring.get("tp_weak_score", 5), f"TP弱突破 量{vol_ratio:.1f}x"

    except Exception as e:
        logger.debug(f"[TP] {symbol} 跳过: {e}")
        return 0, ""  # fail-open
