"""Guard 基类 + 注册表。

Guard 返回 (ok, reason_if_not_ok)。Engine 按 YAML 声明顺序串行执行，任一 False 拒绝。
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import ClassVar, Type

from ..context import TickContext


class Guard(ABC):
    kind: ClassVar[str] = ""

    def __init__(self, params: dict):
        self.params = params or {}

    @abstractmethod
    def check(self, rule: dict, ctx: TickContext) -> tuple[bool, str]: ...

    def describe(self) -> str:
        return f"{self.kind}({self.params})"


_REGISTRY: dict[str, Type[Guard]] = {}


def register(cls: Type[Guard]) -> Type[Guard]:
    if not cls.kind:
        raise ValueError(f"Guard {cls.__name__} 缺少 kind")
    if cls.kind in _REGISTRY:
        raise ValueError(f"Guard kind 重复注册: {cls.kind}")
    _REGISTRY[cls.kind] = cls
    return cls


def build(kind: str, params: dict) -> Guard:
    if kind not in _REGISTRY:
        raise KeyError(f"未知 guard kind: {kind}，已注册: {list(_REGISTRY.keys())}")
    return _REGISTRY[kind](params)


def list_kinds() -> list[str]:
    return sorted(_REGISTRY.keys())
