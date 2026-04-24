"""CooldownGuard：同 rule_id 距上次触发不足 cooldown_sec 则拒绝。"""
import time

from .base import Guard, register
from .. import state as engine_state
from ..context import TickContext


@register
class CooldownGuard(Guard):
    kind = "cooldown"

    def check(self, rule: dict, ctx: TickContext) -> tuple[bool, str]:
        cd = int(rule.get("cooldown_sec", 0))
        if cd <= 0:
            return True, ""
        rid = rule["id"]
        s = engine_state.get(rid)
        last = s.get("last_fire_at")
        if not last:
            return True, ""
        elapsed = ctx.now_ts - int(last)
        if elapsed < cd:
            return False, f"cooldown 还剩 {cd - elapsed}s"
        return True, ""
