"""箱体层 — 根据当前价格判断位置区段"""
from pathlib import Path
import yaml

_CONFIG = None

def _load_config():
    global _CONFIG
    if _CONFIG is None:
        p = Path(__file__).parent.parent / "config.yaml"
        with open(p) as f:
            _CONFIG = yaml.safe_load(f)
    return _CONFIG


def get_zone(price: float) -> dict:
    cfg = _load_config()
    for z in cfg["zones"]:
        if z["lower"] <= price < z["upper"]:
            return z
    return {"name": "unknown", "label": "未知", "action": "?", "emoji": "❓", "lower": 0, "upper": 0}


def distance_to_center(price: float) -> float:
    cfg = _load_config()
    center = cfg["box"]["small"]["center"]
    return (price - center) / center * 100


def is_breach(price: float) -> bool:
    z = get_zone(price)
    return z["action"] == "FORCE_FLAT"
