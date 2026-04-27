"""
Stage 2 RC 层单测（v3.5-minimalist）
运行：python3 -m pytest tests/test_stage2_rc.py -v
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch, MagicMock
from scanner import _calc_ema, rc2_pass, rc4_pass


# ── _calc_ema ──────────────────────────────────────────────────────────────

def test_calc_ema_seed_equals_sma():
    """20 个价格，period=20：seed=SMA(1..20)=10.5，无后续，EMA=10.5"""
    prices = list(range(1, 21))
    ema = _calc_ema(prices, 20)
    assert abs(ema - 10.5) < 0.001

def test_calc_ema_constant():
    """所有价格相等：EMA = 该值"""
    prices = [10.0] * 20
    ema = _calc_ema(prices, 20)
    assert abs(ema - 10.0) < 0.001

def test_calc_ema_fewer_than_period():
    """价格数少于 period：返回均值"""
    prices = [1.0, 2.0, 3.0]
    ema = _calc_ema(prices, 20)
    assert abs(ema - 2.0) < 0.001


# ── rc2_pass ───────────────────────────────────────────────────────────────

def test_rc2_burst_1m():
    ok, reason = rc2_pass(c1m=8.0, c3m=0, c5m=0, c15m=0, vol_ratio=1.0)
    assert ok and "burst_1m" in reason

def test_rc2_burst_3m():
    ok, reason = rc2_pass(c1m=1.0, c3m=6.0, c5m=0, c15m=0, vol_ratio=1.0)
    assert ok and "burst_3m" in reason

def test_rc2_burst_5m():
    ok, reason = rc2_pass(c1m=1.0, c3m=1.0, c5m=4.5, c15m=0, vol_ratio=1.0)
    assert ok and "burst_5m" in reason

def test_rc2_burst_15m():
    ok, reason = rc2_pass(c1m=1.0, c3m=1.0, c5m=1.0, c15m=9.0, vol_ratio=1.0)
    assert ok and "burst_15m" in reason

def test_rc2_stair():
    ok, reason = rc2_pass(c1m=1.0, c3m=1.0, c5m=1.0, c15m=6.5, vol_ratio=2.5)
    assert ok and "stair" in reason

def test_rc2_all_fail():
    ok, reason = rc2_pass(c1m=1.0, c3m=1.0, c5m=1.0, c15m=1.0, vol_ratio=1.0)
    assert not ok and reason == ""


# ── rc4_pass ───────────────────────────────────────────────────────────────

def _make_klines_1h(highs: list) -> list:
    """构造 klines（Binance 格式 [openTime, open, high, low, close, vol, ...]）"""
    return [[str(i * 3600000), "99", str(h), str(h * 0.80), str(h * 0.95), "1000"]
            for i, h in enumerate(highs)]

@patch("scanner._get_klines_cached")
def test_rc4_large_dd_rebounding(mock_klines):
    """回撤 >12% 且当前价 < 最高 ×0.92 → 拦截"""
    mock_klines.return_value = _make_klines_1h([100, 100, 100, 100])
    ticker = {"price": 85.0}   # 回撤 15%, 85 < 100*0.92=92 → 拦
    ok, reason = rc4_pass("XUSDT", ticker)
    assert not ok and "RC-4" in reason

@patch("scanner._get_klines_cached")
def test_rc4_small_dd_pass(mock_klines):
    """回撤 <12% → 放行"""
    mock_klines.return_value = _make_klines_1h([100, 100, 100, 100])
    ticker = {"price": 95.0}   # 回撤 5% < 12% → 放行
    ok, _ = rc4_pass("XUSDT", ticker)
    assert ok

@patch("scanner._get_klines_cached")
def test_rc4_large_dd_already_recovered(mock_klines):
    """回撤 >12% 但已回升到 ≥0.92 → 放行"""
    mock_klines.return_value = _make_klines_1h([100, 100, 100, 100])
    ticker = {"price": 93.0}   # 回撤 7%<12% → 放行（threshold 92, 93≥92 → is_rebounding=False）
    ok, _ = rc4_pass("XUSDT", ticker)
    assert ok

@patch("scanner._get_klines_cached")
def test_rc4_no_klines_fail_open(mock_klines):
    """klines 为空 → fail-open"""
    mock_klines.return_value = []
    ticker = {"price": 50.0}
    ok, _ = rc4_pass("XUSDT", ticker)
    assert ok
