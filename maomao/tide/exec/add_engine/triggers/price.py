"""PriceTrigger / PctTrigger — 自定义绝对价 / 相对涨跌幅触发。

PriceTrigger：
  - direction=up   → cur >= target  → 触发
  - direction=down → cur <= target  → 触发
  require_fresh_cross=true 时只在"本轮刚穿越"那一 tick 触发（和 box_edge 一样去抖）。

PctTrigger：
  - ref: entry / last_sell / last_add / fixed:<price> （参考价来源）
  - direction: up / down
  - threshold_pct: 涨/跌 >= 阈值 → 触发
  - 常用：跌 5% 加多、涨 10% 加空
"""
from .base import Trigger, register
from ..context import TickContext
from .. import state as engine_state


def _fresh_cross(cur_inside_cond: bool, ctx: TickContext, marker: str) -> bool:
    key = f"_cross_inside::{marker}::{ctx.symbol}"
    s = engine_state._load()
    prev = s.get(key, {})
    was_in = bool(prev.get("inside", False))
    s[key] = {"inside": bool(cur_inside_cond)}
    engine_state._save(s)
    return (not was_in) and cur_inside_cond


@register
class PriceTrigger(Trigger):
    kind = "price"

    def should_fire(self, ctx: TickContext) -> bool:
        target = self.params.get("target")
        if target is None:
            return False
        target = float(target)
        direction = self.params.get("direction", "down")
        fresh = bool(self.params.get("require_fresh_cross", True))

        if direction == "up":
            cond = ctx.cur_price >= target
        else:
            cond = ctx.cur_price <= target

        if not fresh:
            return cond
        return _fresh_cross(cond, ctx, marker=f"price::{direction}::{target}")


def _resolve_ref(ref: str, ctx: TickContext) -> float | None:
    if not ref:
        return None
    if ref.startswith("fixed:"):
        try:
            return float(ref.split(":", 1)[1])
        except ValueError:
            return None
    if ref == "entry":
        # 从 positions 取主仓位的 entryPrice（取第一条有效仓位）
        for p in ctx.positions:
            if abs(float(p.get("positionAmt", 0))) > 0:
                return float(p["entryPrice"])
        return None
    if ref == "last_sell":
        ls = ctx.last_sell or {}
        v = ls.get("price")
        return float(v) if v else None
    if ref == "last_add":
        la = ctx.last_add or {}
        v = la.get("price")
        return float(v) if v else None
    return None


@register
class PctTrigger(Trigger):
    kind = "pct"

    def should_fire(self, ctx: TickContext) -> bool:
        ref = self.params.get("ref", "entry")
        direction = self.params.get("direction", "down")
        threshold_pct = float(self.params.get("threshold_pct", 0))
        fresh = bool(self.params.get("require_fresh_cross", True))
        if threshold_pct <= 0:
            return False

        base = _resolve_ref(ref, ctx)
        if base is None or base <= 0:
            return False

        change_pct = (ctx.cur_price - base) / base * 100.0
        if direction == "up":
            cond = change_pct >= threshold_pct
        else:
            cond = change_pct <= -threshold_pct

        if not fresh:
            return cond
        return _fresh_cross(cond, ctx,
                            marker=f"pct::{ref}::{direction}::{threshold_pct}::{base:.4f}")
