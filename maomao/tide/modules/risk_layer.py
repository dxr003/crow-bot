"""风控层 fk1/fk5 — 风控拦截（TODO: fk5待实现）"""

# fk1: 单笔上限（已实现）
# fk5: 强制平仓（TODO: 接 trader/executor 实现）

SINGLE_ORDER_LIMIT = 500.0

def check_single_limit(order_usd: float) -> bool:
    """fk1: 单笔不超上限，返回 True=允许"""
    return order_usd <= SINGLE_ORDER_LIMIT

# TODO fk5: 破箱4因子共振确认后，调 executor.close_market 全平
# 影子盘阶段：只推通知不执行
