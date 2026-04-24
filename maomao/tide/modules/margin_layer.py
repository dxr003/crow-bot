"""保证金层 mr1 — 保证金补给规则（TODO: 待实现）"""

# TODO mr1: 场外保证金池补给规则
# - 在场保证金 < 阈值时从场外池补给
# - 补给条件：当前区段仍为加仓区 + 池内余额充足
# - 影子盘阶段：只记录不执行

REPLENISH_THRESHOLD = 200.0  # 在场保证金低于此值时触发补给

def needs_replenish(state: dict) -> bool:
    in_margin = sum(p.get("usd", 0) for p in state.get("positions", []))
    return in_margin < REPLENISH_THRESHOLD
