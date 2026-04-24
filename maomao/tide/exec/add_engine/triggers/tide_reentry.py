"""BuybackTrigger / PullbackTrigger — 潮汐减仓后的再入场触发。

BuybackTrigger：跌回上次卖出价 N% 以下 → 触发买回（默认 1%）
PullbackTrigger：跌到上次卖出价 M% 以下 → 触发加仓（默认 2%）

数据来源：ctx.last_sell = {"price": float, "sold_usd": float}
由 reduce_layer 触发减仓后写入潮汐 state.json，engine 每轮读出传入 ctx。

说明：
  - 两个触发器逻辑同构，仅默认阈值不同
  - 潮汐做空时 "current < sell_price * (1 - pct/100)" 意味着价格比上次卖出更低 → 回补空仓
  - 本触发器方向无关，只看"跌回"——engine_rule 的 side 字段决定真实下单方向
"""
from .base import Trigger, register
from ..context import TickContext
from .. import state as engine_state


def _fresh_cross(cond: bool, ctx: TickContext, marker: str) -> bool:
    key = f"_tide_reentry::{marker}::{ctx.symbol}"
    s = engine_state._load()
    prev = s.get(key, {})
    was_in = bool(prev.get("inside", False))
    s[key] = {"inside": bool(cond)}
    engine_state._save(s)
    return (not was_in) and cond


def _below_sell(ctx: TickContext, pct: float, marker: str,
                require_fresh: bool) -> bool:
    ls = ctx.last_sell or {}
    sell_price = float(ls.get("price") or 0)
    sold_usd = float(ls.get("sold_usd") or 0)
    if sell_price <= 0 or sold_usd <= 0:
        return False
    trigger_line = sell_price * (1.0 - pct / 100.0)
    cond = ctx.cur_price < trigger_line
    if not require_fresh:
        return cond
    return _fresh_cross(cond, ctx, marker=f"{marker}::{sell_price}::{pct}")


@register
class BuybackTrigger(Trigger):
    kind = "buyback"

    def should_fire(self, ctx: TickContext) -> bool:
        pct = float(self.params.get("pct_below_sell", 1.0))
        fresh = bool(self.params.get("require_fresh_cross", True))
        return _below_sell(ctx, pct, marker="buyback", require_fresh=fresh)


@register
class PullbackTrigger(Trigger):
    kind = "pullback"

    def should_fire(self, ctx: TickContext) -> bool:
        pct = float(self.params.get("pct_below_sell", 2.0))
        fresh = bool(self.params.get("require_fresh_cross", True))
        return _below_sell(ctx, pct, marker="pullback", require_fresh=fresh)
