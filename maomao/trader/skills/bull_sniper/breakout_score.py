"""F层：支撑/阻力突破评分 v1.0
逻辑：获取48根1H K线 → 识别swing high → 判断收盘是否突破 +1.5% → 评分+形态过滤
只在1H收盘后评一次（缓存上次评分时间，每小时最多评一次）
"""
import time
import logging
import urllib.request
import json

logger = logging.getLogger("bull_sniper.breakout_score")

# 每个币的上次评分缓存 {symbol: {"ts": float, "result": dict}}
_cache: dict = {}
_CACHE_TTL = 3500  # 约1小时，3500秒避免整点抖动


def fetch_1h_klines(symbol: str, limit: int = 52) -> list:
    """拉取 Binance fapi 1H K线，返回 list of dict"""
    url = (
        f"https://fapi.binance.com/fapi/v1/klines"
        f"?symbol={symbol}&interval=1h&limit={limit}"
    )
    with urllib.request.urlopen(url, timeout=8) as r:
        raw = json.loads(r.read())
    # raw: [[open_time, open, high, low, close, ...], ...]
    result = []
    for k in raw:
        result.append({
            "ts": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
        })
    return result


def find_swing_highs(klines: list, left: int = 3, right: int = 3) -> list:
    """识别局部高点 swing high，返回 [(index, price), ...]"""
    swings = []
    for i in range(left, len(klines) - right):
        h = klines[i]["high"]
        if all(h > klines[i - j]["high"] for j in range(1, left + 1)) and \
           all(h > klines[i + j]["high"] for j in range(1, right + 1)):
            swings.append((i, h))
    return swings


def candle_shape(k: dict) -> str:
    """判断K线形态"""
    o, h, l, c = k["open"], k["high"], k["low"], k["close"]
    full = h - l
    if full < 1e-9:
        return "doji"
    body = abs(c - o)
    upper_shadow = h - max(o, c)
    body_ratio = body / full
    upper_ratio = upper_shadow / full
    is_bullish = c > o

    if upper_ratio > 0.5:
        return "upper_shadow"   # 长上影，诱多
    if is_bullish and body_ratio > 0.7:
        return "strong_bullish"
    if is_bullish and body_ratio > 0.5:
        return "bullish"
    return "neutral"


def score_breakout_1h(symbol: str) -> dict:
    """
    主入口：返回 {
        "score": int,        # 0-25
        "vetoed": bool,      # 长上影一票否决
        "veto_reason": str,
        "reason": str,       # 用于 breakdown 展示
        "detail": dict,
    }
    """
    zero = {"score": 0, "vetoed": False, "veto_reason": "", "reason": "", "detail": {}}

    # 缓存检查：同一小时内不重复评
    now = time.time()
    cached = _cache.get(symbol)
    if cached and (now - cached["ts"]) < _CACHE_TTL:
        return cached["result"]

    try:
        klines = fetch_1h_klines(symbol, limit=52)
    except Exception as e:
        logger.warning(f"[F层] {symbol} 拉K线失败: {e}")
        return zero

    if len(klines) < 10:
        zero["reason"] = "K线不足"
        return zero

    # 用已收盘的K线（去掉最后一根，可能未收盘）
    closed = klines[:-1]
    if len(closed) < 7:
        return zero

    # 找 swing high（不含最后一根收盘线）
    swings = find_swing_highs(closed[:-1], left=3, right=3)
    if not swings:
        zero["reason"] = "无swing high"
        return zero

    # 阻力位：取最近5个swing high中最高的（避免被低小高点假突破触发）
    recent_swings = swings[-5:]
    nearest_resistance = max(s[1] for s in recent_swings)

    # 最近一根已收盘K线
    latest = closed[-1]
    close_price = latest["close"]

    break_pct = (close_price - nearest_resistance) / nearest_resistance

    detail = {
        "resistance": nearest_resistance,
        "close": close_price,
        "break_pct": round(break_pct * 100, 2),
        "swing_count": len(swings),
    }

    if break_pct <= 0.015:
        # 未突破
        result = {**zero, "reason": f"未突破阻力{nearest_resistance:.2f}({break_pct*100:+.1f}%)", "detail": detail}
        _cache[symbol] = {"ts": now, "result": result}
        return result

    # 已突破，检查形态
    shape = candle_shape(latest)
    detail["shape"] = shape

    if shape == "upper_shadow":
        # 长上影 → veto
        result = {
            "score": 0,
            "vetoed": True,
            "veto_reason": f"F层：突破后长上影(诱多)，拒绝信号",
            "reason": f"F.突破{break_pct*100:.1f}%但长上影veto",
            "detail": detail,
        }
        _cache[symbol] = {"ts": now, "result": result}
        return result

    # 计算得分（v3.4 F层降权：满分15，对应20%权重）
    if break_pct > 0.05:
        base = 12
    elif break_pct > 0.03:
        base = 9
    elif break_pct > 0.015:
        base = 6
    else:
        base = 0

    shape_mult = {
        "strong_bullish": 1.25,
        "bullish": 1.0,
        "neutral": 0.7,
        "doji": 0.3,
    }
    final = int(base * shape_mult.get(shape, 0.7))
    final = min(final, 15)

    result = {
        "score": final,
        "vetoed": False,
        "veto_reason": "",
        "reason": f"F.1H突破+{break_pct*100:.1f}%({shape})",
        "detail": detail,
    }
    _cache[symbol] = {"ts": now, "result": result}
    return result
