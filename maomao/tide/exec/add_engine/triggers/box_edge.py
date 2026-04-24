"""CeilingTrigger / FloorTrigger — 小箱顶/底加仓触发器。

CeilingTrigger：价格进入小箱顶区域（顶下沿 = top*(1 - tolerance_pct/100)）→ 触发
FloorTrigger  ：价格进入小箱底区域（底上沿 = bottom*(1 + tolerance_pct/100)）→ 触发

YAML 参数（两种获取箱沿的方式任选其一，都写时硬编码优先）：
  trigger:
    kind: ceiling          # 或 floor
    box_top: 78000         # 硬编码绝对价
    # source: tide_small_upper / tide_small_lower / tide_mother_upper / tide_mother_lower
    tolerance_pct: 1.0     # 容错 %，默认 1.0
    require_fresh_entry: true  # true=只在"刚进入区域"那一 tick 触发；false=在区域内每 tick 都触发
                               # require_fresh_entry 依赖 engine_state 里的 last_seen_inside 标记
"""
from .base import Trigger, register
from ..context import TickContext
from .. import state as engine_state


_SOURCES = {
    "tide_small_upper":  ("small", "upper"),
    "tide_small_lower":  ("small", "lower"),
    "tide_mother_upper": ("mother", "upper"),
    "tide_mother_lower": ("mother", "lower"),
}


def _resolve_edge(params: dict, hard_key: str, ctx: TickContext) -> float | None:
    v = params.get(hard_key)
    if v is not None:
        return float(v)
    source = params.get("source")
    if source and source in _SOURCES:
        box_key, sub = _SOURCES[source]
        box = (ctx.tide_state or {}).get(box_key) or {}
        val = box.get(sub)
        if val is not None:
            return float(val)
    return None


class _EdgeBase(Trigger):
    """公用：判断是否在区域内 + fresh_entry 去抖。"""

    def _fresh_mark_key(self, ctx: TickContext) -> str:
        # 因为 base class 不知道 rule_id，用 symbol 做粗粒度的 fresh entry marker。
        # 实际 rule 维度的 fresh 由 engine_state.get(rule_id).last_fire_at + cooldown 兜底。
        return f"_edge_inside::{self.kind}::{ctx.symbol}"

    def _check_fresh(self, inside: bool, ctx: TickContext, rule_id_hint: str = "") -> bool:
        """在 add_engine_state 里用一个独立 key 记录"上轮是否已在区域"。
        require_fresh_entry=true 时：上轮在外 + 本轮在内 才返 True。
        """
        key = self._fresh_mark_key(ctx) + f"::{rule_id_hint}"
        prev = engine_state.get(key)
        was_inside = bool(prev.get("inside", False)) if prev else False
        # 用 record_fire 借位存 bool。简单起见：state.py 的 fire_count 字段临时当标记
        # 为避免污染 fire_count，这里直接写一个子键。
        from .. import state as _s
        s = _s._load()
        s[key] = {"inside": bool(inside)}
        _s._save(s)

        if not bool(self.params.get("require_fresh_entry", True)):
            return inside
        return (not was_inside) and inside


@register
class CeilingTrigger(_EdgeBase):
    kind = "ceiling"

    def should_fire(self, ctx: TickContext) -> bool:
        top = _resolve_edge(self.params, "box_top", ctx)
        if top is None:
            return False
        tol_pct = float(self.params.get("tolerance_pct", 1.0))
        entry_line = top * (1.0 - tol_pct / 100.0)
        inside = ctx.cur_price >= entry_line and ctx.cur_price <= top * (1.0 + tol_pct / 100.0)
        return self._check_fresh(inside, ctx, rule_id_hint=f"top{top}")


@register
class FloorTrigger(_EdgeBase):
    kind = "floor"

    def should_fire(self, ctx: TickContext) -> bool:
        bottom = _resolve_edge(self.params, "box_bottom", ctx)
        if bottom is None:
            return False
        tol_pct = float(self.params.get("tolerance_pct", 1.0))
        entry_line = bottom * (1.0 + tol_pct / 100.0)
        inside = ctx.cur_price <= entry_line and ctx.cur_price >= bottom * (1.0 - tol_pct / 100.0)
        return self._check_fresh(inside, ctx, rule_id_hint=f"bot{bottom}")
