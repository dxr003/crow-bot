"""Trigger 基类 + 注册表。

子类约定：
  - kind: 唯一字符串，YAML 里 trigger.kind 引用
  - __init__(self, params: dict)
  - should_fire(self, ctx: TickContext) -> bool
    （返回 True 表示触发，False 表示本轮不动）
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import ClassVar, Type

from ..context import TickContext


class Trigger(ABC):
    kind: ClassVar[str] = ""

    def __init__(self, params: dict):
        self.params = params or {}

    @abstractmethod
    def should_fire(self, ctx: TickContext) -> bool: ...

    def describe(self) -> str:
        return f"{self.kind}({self.params})"


_REGISTRY: dict[str, Type[Trigger]] = {}


def register(cls: Type[Trigger]) -> Type[Trigger]:
    if not cls.kind:
        raise ValueError(f"Trigger {cls.__name__} 缺少 kind")
    if cls.kind in _REGISTRY:
        raise ValueError(f"Trigger kind 重复注册: {cls.kind}")
    _REGISTRY[cls.kind] = cls
    return cls


def build(kind: str, params: dict) -> Trigger:
    if kind not in _REGISTRY:
        raise KeyError(f"未知 trigger kind: {kind}，已注册: {list(_REGISTRY.keys())}")
    return _REGISTRY[kind](params)


def list_kinds() -> list[str]:
    return sorted(_REGISTRY.keys())
