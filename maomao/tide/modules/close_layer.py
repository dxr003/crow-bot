"""平仓层 fk5 — 破箱4因子共振强制平仓"""


def check_force_flat(state: dict, config: dict) -> tuple[bool, str]:
    """fk5 破箱检测：价格出母箱 + 4因子共振才触发"""
    price = state.get("current_price", 0)
    mother_upper = config["box"]["mother"]["upper"]   # 88000
    mother_lower = config["box"]["mother"]["lower"]   # 58000

    if mother_lower <= price <= mother_upper:
        return False, f"in_box|price={price}"

    breach = state.get("breach_factors", {})
    price_breach = breach.get("price_breach", False)
    volume_spike = breach.get("volume_spike", False)
    oi_change    = breach.get("oi_change", False)
    funding_ext  = breach.get("funding_extreme", False)

    factors = sum([price_breach, volume_spike, oi_change, funding_ext])

    if factors >= 4:
        direction = "up" if price > mother_upper else "down"
        return True, f"force_flat|direction={direction}|price={price}|factors={factors}/4"

    return False, f"breach_pending|price={price}|factors={factors}/4"
