"""YAML 规则加载 + 实例化触发器/守门员。

YAML 结构：
engine:
  shadow: true         # true=影子，不真下单，只打 fire 日志
rules:
  - id: demo_never
    enabled: true
    trigger: {kind: demo, fire: false}
    guards:            # 可选，缺省为 [cooldown]
      - {kind: cooldown}
    side: short
    account: 币安2
    margin_usd: 100
    leverage: 3
    cooldown_sec: 900
"""
import os
from pathlib import Path
import yaml

from . import triggers as trig_pkg
from . import guards as grd_pkg

RULES_FILE = Path(__file__).parent / "add_rules.yaml"

_DEFAULT_GUARDS = [{"kind": "cooldown"}]


def _mtime() -> float:
    return RULES_FILE.stat().st_mtime if RULES_FILE.exists() else 0.0


def load_raw() -> dict:
    if not RULES_FILE.exists():
        return {"engine": {"shadow": True}, "rules": []}
    with RULES_FILE.open() as f:
        return yaml.safe_load(f) or {}


def build_rules(raw: dict) -> list[dict]:
    """把 YAML 里的 rule 编译成可执行结构。
    返回 list[{id, enabled, trigger(obj), guards([objs]), side, account, margin_usd, leverage, cooldown_sec, note}]
    """
    out = []
    for r in raw.get("rules") or []:
        if not r.get("enabled", True):
            continue
        rid = r.get("id")
        if not rid:
            raise ValueError(f"rule 缺少 id: {r}")
        t_cfg = r.get("trigger") or {}
        t_kind = t_cfg.get("kind")
        if not t_kind:
            raise ValueError(f"rule {rid} 缺少 trigger.kind")
        trigger = trig_pkg.build(t_kind, {k: v for k, v in t_cfg.items() if k != "kind"})

        g_cfgs = r.get("guards") or _DEFAULT_GUARDS
        guard_objs = [grd_pkg.build(g["kind"], {k: v for k, v in g.items() if k != "kind"})
                      for g in g_cfgs]

        # 透传原 YAML 所有字段，编译产物只覆盖 trigger/guards
        # 这样新加字段（entry_type/limit_*/margin_mode/...）不用改 loader 就能到下游
        compiled = dict(r)
        compiled.update({
            "id": rid,
            "enabled": True,
            "trigger": trigger,
            "guards": guard_objs,
            "side": r.get("side", "long"),
            "symbol": r.get("symbol", "BTCUSDT"),
            "account": r.get("account", "币安2"),
            "margin_usd": r.get("margin_usd", 0),
            "leverage": r.get("leverage", 3),
            "cooldown_sec": r.get("cooldown_sec", 0),
            "note": r.get("note", ""),
        })
        out.append(compiled)
    return out


class RulesCache:
    """支持 mtime 热加载。"""

    def __init__(self):
        self._mtime_seen: float = -1.0
        self._raw: dict = {}
        self._rules: list[dict] = []

    def get(self) -> tuple[dict, list[dict]]:
        m = _mtime()
        if m != self._mtime_seen:
            self._raw = load_raw()
            self._rules = build_rules(self._raw)
            self._mtime_seen = m
        return self._raw, self._rules
