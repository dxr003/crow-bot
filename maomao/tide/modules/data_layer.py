"""数据层 — sj1(K线+价格) / sj3(OI) / sj4(资金费率)"""
import json
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))
STATE_PATH = Path(__file__).parent.parent / "state" / "state.json"
FAPI = "https://fapi.binance.com"


def fetch_price() -> tuple[float, float]:
    """sj1: (last_price, price_change_pct_24h)"""
    url = f"{FAPI}/fapi/v1/ticker/24hr?symbol=BTCUSDT"
    with urllib.request.urlopen(url, timeout=10) as r:
        d = json.loads(r.read())
    return float(d["lastPrice"]), float(d["priceChangePercent"])


def fetch_klines_4h(limit: int = 20) -> list:
    """sj1: 最近N根4H K线，原始列表格式 [open_ts, open, high, low, close, vol, ...]"""
    url = f"{FAPI}/fapi/v1/klines?symbol=BTCUSDT&interval=4h&limit={limit}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def fetch_klines_1m(limit: int = 21) -> list[dict]:
    """sj1: 最近N根1m K线，用于计算量比"""
    url = f"{FAPI}/fapi/v1/klines?symbol=BTCUSDT&interval=1m&limit={limit}"
    with urllib.request.urlopen(url, timeout=10) as r:
        raw = json.loads(r.read())
    return [{"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
             "close": float(k[4]), "volume": float(k[5])} for k in raw]


def fetch_oi() -> tuple[float, float]:
    """sj3: (current_oi, oi_change_pct_vs_prev)
    对比当前OI和5分钟前快照，返回变化百分比"""
    url = f"{FAPI}/fapi/v1/openInterest?symbol=BTCUSDT"
    with urllib.request.urlopen(url, timeout=10) as r:
        cur = float(json.loads(r.read())["openInterest"])

    # 历史OI（5分钟周期，取最近2条）
    url2 = f"{FAPI}/futures/data/openInterestHist?symbol=BTCUSDT&period=5m&limit=2"
    with urllib.request.urlopen(url2, timeout=10) as r:
        hist = json.loads(r.read())
    if len(hist) >= 2:
        prev = float(hist[-2]["sumOpenInterest"])
        change_pct = (cur - prev) / prev * 100 if prev else 0.0
    else:
        change_pct = 0.0
    return cur, change_pct


def fetch_funding_rate() -> float:
    """sj4: 当前资金费率（小数，如 0.0003 = 0.03%）"""
    url = f"{FAPI}/fapi/v1/premiumIndex?symbol=BTCUSDT"
    with urllib.request.urlopen(url, timeout=10) as r:
        d = json.loads(r.read())
    return float(d["lastFundingRate"])


def read_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {
        "current_segment": "unknown",
        "current_price": 0.0,
        "price_change_pct": 0.0,
        "positions": [],
        "last_update": "",
    }


def write_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_PATH)
