"""决策引擎 — 区段 + 持仓状态 → 操作建议（只决策不下单）"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import logging
import yaml

BJ = timezone(timedelta(hours=8))
_CFG = None

def _cfg():
    global _CFG
    if _CFG is None:
        p = Path(__file__).parent.parent / "config.yaml"
        with open(p) as f:
            _CFG = yaml.safe_load(f)
    return _CFG


# 每个 action 对应的建议描述
_ACTION_LABEL = {
    "FORCE_FLAT":  "🚨 全平离场（破箱）",
    "REDUCE_70":   "🔴 减仓 70%",
    "REDUCE_50":   "🟠 减仓 50%",
    "REDUCE_30":   "🟡 减仓 30%",
    "NO_ACTION":   "⚖️ 观望持仓",
    "ADD_1X":      "🟩 加仓 1x base",
    "ADD_1_5X":    "💚 加仓 1.5x base",
    "ADD_2X":      "💙 加仓 2x base",
    "ADD_3X":      "💎 加仓 3x base（需确认）",
    "?":           "❓ 未知区段",
}


def make_decision(price: float, zone: dict, state: dict) -> dict:
    """
    输入：当前价、区段、系统状态
    输出：decision dict {action, label, price, zone_name, reason, timestamp}
    """
    action = zone.get("action", "?")
    positions = state.get("positions", [])
    total_usd = sum(p.get("usd", 0) for p in positions)
    layers = len(positions)

    cfg = _cfg()
    max_layers = cfg.get("position", {}).get("max_layers", 5)
    max_total  = cfg.get("position", {}).get("max_total_usd", 800)
    base_usd   = cfg.get("position", {}).get("base_usd", 100)

    # 加仓检查：是否已达上限
    if action.startswith("ADD"):
        if layers >= max_layers:
            action = "NO_ACTION"
            reason = f"已达最大层数 {max_layers}，跳过加仓"
        elif total_usd >= max_total:
            action = "NO_ACTION"
            reason = f"总仓位 ${total_usd:.0f} 已达上限 ${max_total}，跳过加仓"
        else:
            multiplier = {"ADD_1X": 1.0, "ADD_1_5X": 1.5, "ADD_2X": 2.0, "ADD_3X": 3.0}.get(action, 1.0)
            add_usd = base_usd * multiplier
            reason = f"当前层数 {layers}/{max_layers}，建议加仓 ${add_usd:.0f}"
    elif action.startswith("REDUCE"):
        if not positions:
            action = "NO_ACTION"
            reason = "无持仓，跳过减仓"
        else:
            pct = {"REDUCE_30": 30, "REDUCE_50": 50, "REDUCE_70": 70}.get(action, 0)
            reduce_usd = total_usd * pct / 100
            reason = f"总仓位 ${total_usd:.0f}，建议平仓 ${reduce_usd:.0f}（{pct}%）"
    elif action == "FORCE_FLAT":
        reason = "母箱破位，建议全平"
    else:
        reason = f"区段={zone.get('label','?')}，维持当前仓位"

    return {
        "action": action,
        "label": _ACTION_LABEL.get(action, action),
        "price": price,
        "zone_name": zone.get("name", "?"),
        "zone_label": zone.get("label", "?"),
        "reason": reason,
        "timestamp": datetime.now(BJ).isoformat(),
    }
