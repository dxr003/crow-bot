"""add_engine 的持久化状态：每条 rule 的 last_fire_at / last_fire_price / fire_count。

key 格式：rule_id
值：{"last_fire_at": epoch, "last_fire_price": float, "fire_count": int, "total_margin_usd": float}
"""
import json
import os
import time
from pathlib import Path

STATE_FILE = Path("/root/maomao/tide/data/add_engine_state.json")


def _load() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save(s: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2))
    os.replace(tmp, STATE_FILE)


def get(rule_id: str) -> dict:
    s = _load()
    return s.get(rule_id, {})


def record_fire(rule_id: str, fire_price: float, margin_usd: float):
    s = _load()
    entry = s.get(rule_id, {"fire_count": 0, "total_margin_usd": 0.0})
    entry["last_fire_at"] = int(time.time())
    entry["last_fire_price"] = float(fire_price)
    entry["fire_count"] = int(entry.get("fire_count", 0)) + 1
    entry["total_margin_usd"] = float(entry.get("total_margin_usd", 0.0)) + float(margin_usd)
    s[rule_id] = entry
    _save(s)


def reset(rule_id: str | None = None):
    if rule_id is None:
        _save({})
        return
    s = _load()
    s.pop(rule_id, None)
    _save(s)


def dump() -> dict:
    return _load()
