"""数据层 — sj1(K线+价格) / sj3(OI) / sj4(资金费率)
2026-04-27 Step 6-B: 全部走 api_hub.binance.fapi 统一封装层"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))
STATE_PATH = Path(__file__).parent.parent / "state" / "state.json"

# api_hub 引用（防御性 sys.path）
if "/root/maomao" not in sys.path:
    sys.path.insert(0, "/root/maomao")
from trader.api_hub.binance import fapi


def fetch_price() -> tuple[float, float]:
    """sj1: (last_price, price_change_pct_24h)"""
    d = fapi.get_ticker_24hr("BTCUSDT")
    return float(d["lastPrice"]), float(d["priceChangePercent"])


def fetch_klines_4h(limit: int = 20) -> list:
    """sj1: 最近N根4H K线，原始列表格式 [open_ts, open, high, low, close, vol, ...]"""
    return fapi.get_klines("BTCUSDT", "4h", limit=limit)


def fetch_klines_1m(limit: int = 21) -> list[dict]:
    """sj1: 最近N根1m K线，用于计算量比"""
    raw = fapi.get_klines("BTCUSDT", "1m", limit=limit)
    return [{"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
             "close": float(k[4]), "volume": float(k[5])} for k in raw]


def fetch_oi() -> tuple[float, float]:
    """sj3: (current_oi, oi_change_pct_vs_prev)
    对比当前OI和5分钟前快照，返回变化百分比"""
    cur = float(fapi.get_open_interest("BTCUSDT")["openInterest"])
    hist = fapi.get_open_interest_hist("BTCUSDT", period="5m", limit=2)
    if len(hist) >= 2:
        prev = float(hist[-2]["sumOpenInterest"])
        change_pct = (cur - prev) / prev * 100 if prev else 0.0
    else:
        change_pct = 0.0
    return cur, change_pct


def fetch_funding_rate() -> float:
    """sj4: 当前资金费率（小数，如 0.0003 = 0.03%）"""
    return float(fapi.get_premium_index("BTCUSDT")["lastFundingRate"])


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
