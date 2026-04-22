"""数据层 — 拉 BTC 实时价格"""
import json
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))
STATE_PATH = Path(__file__).parent.parent / "state" / "state.json"


def fetch_price() -> tuple[float, float]:
    """返回 (last_price, price_change_pct_24h)"""
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=BTCUSDT"
    with urllib.request.urlopen(url, timeout=10) as r:
        d = json.loads(r.read())
    return float(d["lastPrice"]), float(d["priceChangePercent"])


def read_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {
        "current_mode": "ms1_normal",
        "current_segment": "unknown",
        "current_price": 0.0,
        "price_change_pct": 0.0,
        "positions": [],
        "last_update": ""
    }


def write_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_PATH)
