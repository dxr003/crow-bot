"""Demo trigger：固定返回 False 或 True，用来跑骨架单测。

YAML 用法：
  trigger:
    kind: demo
    fire: false   # 永不触发（骨架默认）
"""
from .base import Trigger, register
from ..context import TickContext


@register
class DemoTrigger(Trigger):
    kind = "demo"

    def should_fire(self, ctx: TickContext) -> bool:
        return bool(self.params.get("fire", False))
