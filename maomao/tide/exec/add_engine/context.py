"""每轮 tick 的上下文，传给 Trigger / Guard。"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TickContext:
    now_ts: int
    cur_price: float
    mark_price: float
    symbol: str
    positions: list[dict] = field(default_factory=list)
    last_sell: dict | None = None
    last_add: dict | None = None
    engine_state: dict = field(default_factory=dict)
    tide_state: dict = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def position_side(self, side: str) -> dict | None:
        want = "LONG" if side == "long" else "SHORT"
        for p in self.positions:
            if p.get("positionSide") == want and abs(float(p.get("positionAmt", 0))) > 0:
                return p
        return None
