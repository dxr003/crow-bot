"""LiqSafetyGuard — 加仓后强平价安全守门员。

估算"这笔加仓落下去之后，仓位合并均价对应的强平价"是否越界：
  做多：加仓后 liq <= mother_lower （价格得跌破母箱下沿才爆）
  做空：加仓后 liq >= mother_upper （价格得涨破母箱上沿才爆）

当前是近似口径（MMR=0.005，不考虑维持保证金分档和未实现盈亏），
用于影子/粗防线。真实强平价以交易所返回为准，不做强一致性。

YAML 参数（都可选，缺失走默认）：
  mother_lower: 58000
  mother_upper: 88000
  mmr: 0.005
  enforce_long_lower: true   # 做多时校验 liq<=mother_lower
  enforce_short_upper: true  # 做空时校验 liq>=mother_upper
"""
from .base import Guard, register
from ..context import TickContext


def _calc_liq(side: str, avg_entry: float, leverage: int, mmr: float) -> float:
    lev = max(int(leverage or 1), 1)
    if side == "long":
        # 价格下跌爆仓：liq = entry × (1 - 1/lev + mmr)
        return avg_entry * (1 - 1.0 / lev + mmr)
    else:
        # 做空，价格上涨爆仓
        return avg_entry * (1 + 1.0 / lev - mmr)


@register
class LiqSafetyGuard(Guard):
    kind = "liq_safety"

    def check(self, rule: dict, ctx: TickContext) -> tuple[bool, str]:
        side = rule.get("side", "long")
        lev = int(rule.get("leverage", 3))
        margin = float(rule.get("margin_usd", 0) or 0)
        if margin <= 0 or lev <= 0:
            return True, ""  # margin 校验交给 executor_bridge

        mother_lower = float(self.params.get("mother_lower", 58000))
        mother_upper = float(self.params.get("mother_upper", 88000))
        mmr = float(self.params.get("mmr", 0.005))
        enforce_long = bool(self.params.get("enforce_long_lower", True))
        enforce_short = bool(self.params.get("enforce_short_upper", True))

        cur = ctx.cur_price or ctx.mark_price
        if cur <= 0:
            return False, "cur_price<=0"

        pos = ctx.position_side(side)
        qty_old = abs(float(pos["positionAmt"])) if pos else 0.0
        entry_old = float(pos["entryPrice"]) if pos else cur
        qty_new = (margin * lev) / cur

        total_qty = qty_old + qty_new
        if total_qty <= 0:
            return False, "total_qty<=0"
        avg_entry = (qty_old * entry_old + qty_new * cur) / total_qty

        liq = _calc_liq(side, avg_entry, lev, mmr)

        if side == "long" and enforce_long and liq > mother_lower:
            return False, (f"long liq={liq:.2f} > mother_lower={mother_lower} "
                           f"(avg_entry={avg_entry:.2f} lev={lev}x)")
        if side == "short" and enforce_short and liq < mother_upper:
            return False, (f"short liq={liq:.2f} < mother_upper={mother_upper} "
                           f"(avg_entry={avg_entry:.2f} lev={lev}x)")

        return True, ""
