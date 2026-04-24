"""QuotaGuard — 场外额度守门员。

检查这笔加仓要占用的保证金 vs 潮汐 state 里声明的 `remaining_usd`。
超额 / 剩余池被打穿 → 拒绝。

数据来源：ctx.tide_state.position_structure.remaining_usd
margin 来源：优先 ctx.extra["margin_effective"]（engine 已解析），否则回退 rule.margin_usd

参数（guard 级，写在 rule.guards[n] 里）：
  - margin_min_remaining_usd: float   # 开这笔后 remaining 至少还剩多少（默认 0）
  - max_pct_of_remaining:     float   # 单笔最多占 remaining 的百分比（默认 100）
  - require_position_structure: bool  # 没 tide_state.position_structure 时直接拒（默认 True）

示例：
  guards:
    - kind: quota
      max_pct_of_remaining: 50      # 单笔不超过剩余池 50%
      margin_min_remaining_usd: 100 # 开完至少还剩 100U
"""
from .base import Guard, register
from ..context import TickContext


@register
class QuotaGuard(Guard):
    kind = "quota"

    def check(self, rule: dict, ctx: TickContext) -> tuple[bool, str]:
        margin = float(ctx.extra.get("margin_effective", 0) or 0)
        if margin <= 0:
            margin = float(rule.get("margin_usd", 0) or 0)
        if margin <= 0:
            reason = ctx.extra.get("margin_reason") or "margin_usd 未解析出正值"
            return False, f"quota: 无有效 margin（{reason}）"

        ps = ((ctx.tide_state or {}).get("position_structure") or {})
        if not ps and bool(self.params.get("require_position_structure", True)):
            return False, "quota: tide_state 缺 position_structure"

        remaining = float(ps.get("remaining_usd", 0) or 0)
        min_remaining = float(self.params.get("margin_min_remaining_usd", 0) or 0)
        max_pct = float(self.params.get("max_pct_of_remaining", 100) or 100)

        if remaining <= 0:
            return False, f"quota: remaining_usd={remaining} 已用完"

        if margin > remaining:
            return False, (f"quota: margin={margin:.2f}U > remaining={remaining:.2f}U")

        if max_pct < 100:
            allowed = remaining * max_pct / 100.0
            if margin > allowed:
                return False, (f"quota: margin={margin:.2f}U 超过单笔上限"
                               f"{max_pct}%×{remaining:.2f}={allowed:.2f}U")

        after = remaining - margin
        if after < min_remaining:
            return False, (f"quota: 开完剩 {after:.2f}U < 下限 {min_remaining}U")

        return True, ""
