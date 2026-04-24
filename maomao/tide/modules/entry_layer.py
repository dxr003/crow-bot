"""入场层 rc1 — 首次入场触发"""
import re


def should_enter(decision: dict, state: dict) -> tuple[bool, str]:
    action = decision.get("action", "")
    positions = state.get("positions", [])
    if positions:
        return False, "already_in_position"
    if action in ("ADD_1X", "ADD_1_5X", "ADD_2X", "ADD_3X"):
        return True, action
    return False, f"not_entry_zone:{action}"


def on_enter(state: dict, action: str, total_usd: float = 100.0) -> dict:
    """入场后同步更新 position_structure 和 positions"""
    base_usd  = round(total_usd * 0.30, 2)
    trade_usd = round(total_usd * 0.70, 2)

    state["position_structure"]["base_usd"]  = base_usd
    state["position_structure"]["trade_usd"] = trade_usd
    state["position_structure"]["total_usd"] = total_usd

    state["positions"].append({
        "type": "base",
        "usd": base_usd,
        "entry_price": state["current_price"],
        "layer": 1
    })
    state["positions"].append({
        "type": "trade",
        "usd": trade_usd,
        "entry_price": state["current_price"],
        "layer": 1
    })

    return state


def parse_manual_order(cmd: str, total_capital: float = 500.0) -> dict:
    """解析乌鸦人工指令。格式: 开多 20% 强平价58000 / 开空 20% 强平价100000"""
    direction = "long" if "多" in cmd else "short"
    pct = float(re.search(r'(\d+)%', cmd).group(1)) / 100
    liq = float(re.search(r'强平价(\d+)', cmd).group(1))

    entry_usd  = round(total_capital * pct, 2)
    remaining  = round(total_capital - entry_usd, 2)

    return {
        "direction":       direction,
        "entry_usd":       entry_usd,
        "entry_pct":       pct,
        "liq_price_limit": liq,
        "remaining_usd":   remaining,
        "total_capital":   total_capital,
        "status":          "pending",
    }
