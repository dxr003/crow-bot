"""加仓层 jc1 — 加仓触发条件"""
from datetime import datetime, timezone


def calc_add_usd(action: str, base_usd: float = 100.0) -> float:
    mult = {"ADD_1X": 1.0, "ADD_1_5X": 1.5, "ADD_2X": 2.0, "ADD_3X": 3.0}
    return base_usd * mult.get(action, 1.0)



LEVERAGE = 3


def calc_liq_price(avg_entry: float) -> float:
    return round(avg_entry * (1 - 1 / LEVERAGE), 1)


def safe_to_add(state: dict, new_usd: float, mother_lower: float = 58000) -> tuple[bool, float]:
    """fk5守门员：按固定杠杆反推加仓后均价对应强平价，超过母箱下沿则拒绝"""
    positions = state.get("positions", [])
    current_price = state["current_price"]

    all_entries = [p["entry_price"] for p in positions]
    all_entries.append(current_price)
    avg_entry = sum(all_entries) / len(all_entries)

    liq = calc_liq_price(avg_entry)
    safe = liq < mother_lower

    return safe, liq


def should_buyback(state: dict) -> tuple[bool, float, str]:
    """跌回上次卖出价 1% 以下时触发买回"""
    last_sell = state.get("last_sell", {})
    sell_price = last_sell.get("price", 0)
    sold_usd   = last_sell.get("sold_usd", 0)
    current    = state.get("current_price", 0)

    if sell_price == 0 or sold_usd == 0:
        return False, 0, "no_last_sell"

    trigger = sell_price * 0.99
    if current < trigger:
        return True, sold_usd, f"buyback|sell={sell_price}|now={current}|usd={sold_usd}"

    return False, 0, f"not_yet|sell={sell_price}|trigger={trigger}|now={current}"


def should_add(decision: dict, state: dict) -> tuple[bool, str]:
    """jc1: 加仓触发条件 — 有持仓 + ADD区段 + 有余量 + 频率 + 守门员"""
    action = decision.get("action", "")
    positions = state.get("positions", [])
    ps = state.get("position_structure", {})

    if not positions:
        return False, "no_position_use_rc1"

    if not action.startswith("ADD"):
        return False, f"not_add_zone:{action}"

    remaining = ps.get("remaining_usd", 0)
    if remaining <= 0:
        return False, "no_remaining_capital"

    current_price = state.get("current_price", 0)
    last_sell = state.get("last_sell", {})
    sell_price = last_sell.get("price", 0)
    if sell_price > 0:
        if current_price > sell_price * 0.98:
            return False, f"not_pulled_back_yet|sell={sell_price}|now={current_price}"

    add_usd = calc_add_usd(action)
    safe, liq = safe_to_add(state, add_usd)
    if not safe:
        return False, f"liq_too_close:{liq}"

    return True, f"add:{action}|usd={add_usd}|liq={liq}"
