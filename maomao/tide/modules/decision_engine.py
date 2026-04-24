"""决策引擎 — 区段 + 破箱4因子共振 → 操作建议（只决策不下单）"""
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))

_ACTION_LABEL = {
    "FORCE_FLAT":    "🚨 全平离场（破箱）",
    "REDUCE_70":     "🔴 减仓 70%",
    "REDUCE_50":     "🟠 减仓 50%",
    "REDUCE_30":     "🟡 减仓 30%",
    "NO_NEW_ACTION": "⚖️ 持仓不动",
    "NO_ACTION":     "👁 观望",
    "ADD_1X":        "🟩 加仓 1x",
    "ADD_1_5X":      "💚 加仓 1.5x",
    "ADD_2X":        "💙 加仓 2x",
    "ADD_3X":        "💎 加仓 3x",
    "?":             "❓ 未知区段",
}

# 破箱4因子阈值
_BREAKOUT_PRICE_PCT = 1.5    # 实体收盘突破≥1.5%
_BREAKOUT_VOL_RATIO = 2.0    # 量比≥2x（相对前20根1m均量）
_BREAKOUT_OI_PCT    = 5.0    # OI变化≥5%
_BREAKOUT_FUNDING   = 0.0005 # 资金费率极端≥0.05%


def check_breakout_resonance(price: float, zone: dict,
                              klines: list[dict],
                              oi_change_pct: float,
                              funding_rate: float) -> tuple[bool, str]:
    """
    破箱4因子共振检查。4个条件必须同时满足才算真破箱。
    返回 (is_confirmed, reason_str)
    """
    if zone["action"] != "FORCE_FLAT":
        return False, ""

    reasons = []
    passed = 0

    # 条件1: 价格突破（已由区段判断隐含，这里用相对箱体边界计算）
    boundary = zone["lower"] if price >= zone["lower"] else zone["upper"]
    if boundary > 0:
        breach_pct = abs(price - boundary) / boundary * 100
    else:
        breach_pct = 0.0
    if breach_pct >= _BREAKOUT_PRICE_PCT:
        passed += 1
        reasons.append(f"突破{breach_pct:.1f}%✓")
    else:
        reasons.append(f"突破{breach_pct:.1f}%✗(需≥{_BREAKOUT_PRICE_PCT}%)")

    # 条件2: 量比≥2x
    if len(klines) >= 2:
        last_vol = klines[-1]["volume"]
        avg_vol = sum(k["volume"] for k in klines[:-1]) / len(klines[:-1])
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 0
    else:
        vol_ratio = 0
    if vol_ratio >= _BREAKOUT_VOL_RATIO:
        passed += 1
        reasons.append(f"量比{vol_ratio:.1f}x✓")
    else:
        reasons.append(f"量比{vol_ratio:.1f}x✗(需≥{_BREAKOUT_VOL_RATIO}x)")

    # 条件3: OI变化≥5%
    if abs(oi_change_pct) >= _BREAKOUT_OI_PCT:
        passed += 1
        reasons.append(f"OI{oi_change_pct:+.1f}%✓")
    else:
        reasons.append(f"OI{oi_change_pct:+.1f}%✗(需≥{_BREAKOUT_OI_PCT}%)")

    # 条件4: 资金费率极端
    if abs(funding_rate) >= _BREAKOUT_FUNDING:
        passed += 1
        reasons.append(f"费率{funding_rate*100:.3f}%✓")
    else:
        reasons.append(f"费率{funding_rate*100:.3f}%✗(需≥{_BREAKOUT_FUNDING*100:.3f}%)")

    confirmed = (passed == 4)
    return confirmed, " | ".join(reasons)


def make_decision(price: float, zone: dict, state: dict,
                  klines: list[dict] = None,
                  oi_change_pct: float = 0.0,
                  funding_rate: float = 0.0) -> dict:
    """
    输入：当前价、区段、系统状态 + 可选数据因子
    输出：decision dict
    """
    action = zone.get("action", "?")
    positions = state.get("positions", [])
    total_usd = sum(p.get("usd", 0) for p in positions)
    base_usd = 100

    resonance_confirmed = False
    resonance_detail = ""

    # 破箱区段：必须4因子共振才执行
    if action == "FORCE_FLAT":
        if klines:
            resonance_confirmed, resonance_detail = check_breakout_resonance(
                price, zone, klines, oi_change_pct, funding_rate
            )
        if not resonance_confirmed:
            action = "NO_NEW_ACTION"
            reason = f"进入破箱区段但4因子未共振，继续观察 [{resonance_detail}]"
        else:
            reason = f"母箱破位确认（4因子共振）[{resonance_detail}]"

    elif action.startswith("ADD"):
        multiplier = {"ADD_1X": 1.0, "ADD_1_5X": 1.5, "ADD_2X": 2.0, "ADD_3X": 3.0}.get(action, 1.0)
        add_usd = base_usd * multiplier
        reason = f"加仓 ${add_usd:.0f}（{zone['label']}）"

    elif action.startswith("REDUCE"):
        if not positions:
            action = "NO_ACTION"
            reason = "无持仓，跳过减仓"
        else:
            pct = {"REDUCE_30": 30, "REDUCE_50": 50, "REDUCE_70": 70}.get(action, 0)
            reduce_usd = total_usd * pct / 100
            reason = f"减仓 ${reduce_usd:.0f}（{pct}%，总仓 ${total_usd:.0f}）"

    elif action == "NO_NEW_ACTION":
        reason = f"中心轴区域，持仓不动"
    else:
        reason = f"{zone.get('label', '?')}，观望"

    return {
        "action": action,
        "label": _ACTION_LABEL.get(action, action),
        "price": price,
        "zone_name": zone.get("name", "?"),
        "zone_label": zone.get("label", "?"),
        "zone_emoji": zone.get("emoji", ""),
        "reason": reason,
        "resonance_confirmed": resonance_confirmed,
        "timestamp": datetime.now(BJ).isoformat(),
    }
