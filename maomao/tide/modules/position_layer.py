"""仓位层 cw1/cw2/cw4 — 仓位监控（TODO: 待实现）"""

# cw1: 在场保证金上限 $1000
# cw2: 场外保证金池 $3000
# cw4: 总仓位监控

IN_MARGIN_LIMIT = 1000.0
RESERVE_POOL    = 3000.0

def get_total_usd(state: dict) -> float:
    return sum(p.get("usd", 0) for p in state.get("positions", []))

def is_at_limit(state: dict) -> bool:
    return get_total_usd(state) >= IN_MARGIN_LIMIT

# TODO cw4: 超限报警推通知
