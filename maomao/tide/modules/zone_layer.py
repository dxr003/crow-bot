"""箱体层 xt1 — 11区段位置判断（箱体参数固定不动）"""

# 母箱 $58K-$88K，小箱自动计算（xt7），中心轴 $75.5K
# 按优先级从高到低排列，第一个命中的区段生效
ZONES = [
    {"name": "above_mother",  "lower": 88000, "upper": float("inf"), "action": "FORCE_FLAT",   "label": "母箱破上", "emoji": "🚨"},
    {"name": "far_shore",     "lower": 79000, "upper": 88000,        "action": "REDUCE_70",    "label": "对岸区",   "emoji": "🔴"},
    {"name": "near_shore",    "lower": 76000, "upper": 79000,        "action": "REDUCE_50",    "label": "接近对岸", "emoji": "🟠"},
    {"name": "past_center",   "lower": 75500, "upper": 76000,        "action": "REDUCE_30",    "label": "过中点",   "emoji": "🟡"},
    {"name": "center_axis",   "lower": 74500, "upper": 75500,        "action": "NO_NEW_ACTION","label": "中心轴",   "emoji": "⚖️"},
    {"name": "lower_half",    "lower": 70000, "upper": 74500,        "action": "NO_ACTION",    "label": "下半区",   "emoji": "👁"},
    {"name": "lower_edge",    "lower": 65000, "upper": 70000,        "action": "ADD_1X",       "label": "下沿",     "emoji": "🟩"},
    {"name": "mother_top",    "lower": 60000, "upper": 65000,        "action": "ADD_1_5X",     "label": "母箱顶",   "emoji": "💚"},
    {"name": "mother_mid",    "lower": 55000, "upper": 60000,        "action": "ADD_2X",       "label": "母箱中",   "emoji": "💙"},
    {"name": "mother_bottom", "lower": 50000, "upper": 55000,        "action": "ADD_3X",       "label": "母箱底",   "emoji": "💎"},
    {"name": "below_mother",  "lower": 0,     "upper": 58000,        "action": "FORCE_FLAT",   "label": "母箱破下", "emoji": "🚨"},
]

CENTER = 75500.0


def calc_small_box(candles_4h: list, mother_upper=88000, mother_lower=58000) -> dict:
    """xt7: 根据最近20根4H K线自动计算小箱范围，约束在母箱内"""
    highs = [float(c[2]) for c in candles_4h[-20:]]
    lows  = [float(c[3]) for c in candles_4h[-20:]]
    box_upper = min(max(highs), mother_upper)
    box_lower = max(min(lows),  mother_lower)
    box_mid   = (box_upper + box_lower) / 2
    return {
        "small_box_upper": box_upper,
        "small_box_lower": box_lower,
        "small_box_mid":   box_mid,
    }


def get_zone(price: float) -> dict:
    """xt1: 返回当前价格所在区段"""
    for z in ZONES:
        if z["lower"] <= price < z["upper"]:
            return z
    return {"name": "unknown", "label": "未知", "action": "NO_ACTION", "emoji": "❓",
            "lower": 0, "upper": 0}


def distance_to_center(price: float) -> float:
    """偏离中心轴百分比（正=上方，负=下方）"""
    return (price - CENTER) / CENTER * 100


def is_breakout_zone(zone: dict) -> bool:
    """是否处于破箱区段（需要4因子共振确认）"""
    return zone["action"] == "FORCE_FLAT"
