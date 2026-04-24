"""减仓层 js1 — 减仓触发条件"""

def should_reduce(decision: dict, state: dict) -> tuple[bool, str]:
    action = decision.get("action", "")
    positions = state.get("positions", [])

    if not positions:
        return False, "no_position"

    if action in ("REDUCE_30", "REDUCE_50", "REDUCE_70", "FORCE_FLAT"):
        return True, action

    return False, f"not_reduce_zone:{action}"


def check_trailing_stop(state: dict,
                        profit_trigger: float = 0.10,
                        trail_pct: float = 0.20,
                        reduce_ratio: float = 0.30,
                        tolerance: float = 0.005) -> tuple[bool, str, float]:
    """底仓分级止盈：峰值浮盈≥10%后，从峰值回撤20%→止盈30%底仓，容错±0.5%"""
    positions = state.get("positions", [])
    base_positions = [p for p in positions if p.get("type") == "base"]

    if not base_positions:
        return False, "no_base_position", 0

    current = state.get("current_price", 0)
    peak = state.get("price_peak", 0)
    avg_entry = sum(p["entry_price"] for p in base_positions) / len(base_positions)

    float_pnl = (current - avg_entry) / avg_entry
    peak_pnl  = (peak - avg_entry) / avg_entry

    if peak_pnl < profit_trigger:
        return False, f"profit_not_enough|peak_pnl={peak_pnl:.1%}", 0

    if float_pnl <= 0:
        return False, f"in_loss|pnl={float_pnl:.1%}", 0

    pullback = (peak_pnl - float_pnl) / peak_pnl
    if pullback >= trail_pct:
        base_usd = state.get("position_structure", {}).get("base_usd", 0)
        sell_usd = round(base_usd * reduce_ratio, 2)
        trigger_price = round(current * (1 + tolerance), 1)
        return True, (
            f"trail_stop|entry={avg_entry}|peak={peak}"
            f"|pnl={float_pnl:.1%}|pullback={pullback:.1%}"
            f"|sell={sell_usd}U|trigger={trigger_price}|tolerance=±0.5%"
        ), sell_usd

    return False, f"holding|pullback={pullback:.1%}", 0


def record_sell(state: dict, sell_price: float, sold_usd: float) -> dict:
    """减仓成功后记录卖出价和金额，供买回判断使用"""
    state["last_sell"] = {
        "price": sell_price,
        "sold_usd": sold_usd,
    }
    return state


def calc_reduce_usd(action: str, state: dict) -> float:
    ps = state.get("position_structure", {})
    pct = {"REDUCE_30": 0.30, "REDUCE_50": 0.50, "REDUCE_70": 0.70}
    if action == "FORCE_FLAT":
        return float(ps.get("total_usd", 0))
    trade_usd = float(ps.get("trade_usd", 0))
    return trade_usd * pct.get(action, 0.0)
