"""
Stage 3.3 TP 因子单测（v3.5-minimalist）
运行：python3 -m pytest tests/test_stage3_tp.py -v
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch
from tp_score import score_tp

# Binance kline 格式: [openTime, open, high, low, close, volume, ...]
def _k(high, low, close, volume):
    return [0, str((high + low) / 2), str(high), str(low), str(close), str(volume),
            0, 0, 0, 0, 0, 0]


def _cfg(**overrides):
    base = {
        "scoring": {
            "tp_lookback_1h": 6,
            "tp_breakout_margin": 0.003,
            "tp_wick_ratio_max": 0.40,
            "tp_consolidation_range": 0.08,
            "tp_cache_minutes": 60,
            "tp_super_score": 20,
            "tp_strong_score": 15,
            "tp_normal_score": 10,
            "tp_weak_score": 5,
        }
    }
    base["scoring"].update(overrides)
    return base


def _make_klines(lookback_closes, lookback_vols,
                 cur_close, cur_high, cur_low, cur_vol,
                 prev_close):
    """
    构造 8 根 klines：前 6 根(lookback) + 前一根(prev) + 当前根(current)
    """
    klines = []
    for i, (c, v) in enumerate(zip(lookback_closes, lookback_vols)):
        klines.append(_k(high=c * 1.01, low=c * 0.99, close=c, volume=v))
    klines.append(_k(high=prev_close * 1.005, low=prev_close * 0.995,
                     close=prev_close, volume=1000))
    klines.append(_k(high=cur_high, low=cur_low, close=cur_close, volume=cur_vol))
    return klines


# 基准场景：6 根横盘收在 100，当前突破到 104，量 2x，上影小
_LB_CLOSES = [100.0] * 6
_LB_VOLS   = [1000.0] * 6
_PREV_CLOSE = 99.5   # < 100，首次突破条件成立
_CUR_CLOSE  = 104.0  # > 100 × 1.003 = 100.3，突破成立
_CUR_HIGH   = 104.5  # wick = (104.5-104) / (104.5-101) = 0.5/3.5 ≈ 14% < 40%
_CUR_LOW    = 101.0
_CUR_VOL    = 2000.0  # vol_ratio = 2000/1000 = 2x → strong


def _base_klines(**kw):
    d = dict(lookback_closes=_LB_CLOSES, lookback_vols=_LB_VOLS,
             cur_close=_CUR_CLOSE, cur_high=_CUR_HIGH, cur_low=_CUR_LOW,
             cur_vol=_CUR_VOL, prev_close=_PREV_CLOSE)
    d.update(kw)
    return _make_klines(**d)


# ── fail-open 边界 ──

@patch("tp_score._fetch_klines")
def test_tp_insufficient_klines(mock_fetch):
    """不足 7 根 K 线 → fail-open → 0"""
    mock_fetch.return_value = _make_klines(
        [100.0] * 3, [1000.0] * 3,
        cur_close=104, cur_high=105, cur_low=101, cur_vol=2000, prev_close=99
    )  # 只有 5 根 (3+1+1)
    pts, _ = score_tp("X", _cfg())
    assert pts == 0


# ── 前置门 1：收盘未突破 ──

@patch("tp_score._fetch_klines")
def test_tp_gate1_no_breakout(mock_fetch):
    """当前收盘 < prev_high * 1.003 → 0"""
    mock_fetch.return_value = _base_klines(cur_close=100.1, cur_high=100.5, cur_low=99.5)
    pts, _ = score_tp("X", _cfg())
    assert pts == 0


# ── 前置门 4：非首次突破 ──

@patch("tp_score._fetch_klines")
def test_tp_gate4_not_first_breakout(mock_fetch):
    """前一根 close >= prev_high（已突破）→ 0"""
    mock_fetch.return_value = _base_klines(prev_close=101.0)  # 101 >= 100
    pts, _ = score_tp("X", _cfg())
    assert pts == 0


# ── 前置门 3：上影过大 ──

@patch("tp_score._fetch_klines")
def test_tp_gate3_wick_too_big(mock_fetch):
    """上影 = (high-close)/(high-low) ≥ 40% → 0"""
    # high=110, close=104, low=101 → wick=(110-104)/(110-101)=6/9≈67% > 40%
    mock_fetch.return_value = _base_klines(cur_high=110.0, cur_close=104.0, cur_low=101.0)
    pts, _ = score_tp("X", _cfg())
    assert pts == 0


# ── 4 个档位 ──

@patch("tp_score._fetch_klines")
def test_tp_weak_tier(mock_fetch):
    """所有前置门通过，量 1.2x < 1.5x → 弱突破 = 5"""
    mock_fetch.return_value = _base_klines(cur_vol=1200.0)  # 1200/1000=1.2x
    pts, reason = score_tp("X", _cfg())
    assert pts == 5
    assert "弱突破" in reason


@patch("tp_score._fetch_klines")
def test_tp_normal_tier(mock_fetch):
    """量 1.6x → normal = 10"""
    mock_fetch.return_value = _base_klines(cur_vol=1600.0)
    pts, reason = score_tp("X", _cfg())
    assert pts == 10
    assert "TP突破" in reason and "弱" not in reason


@patch("tp_score._fetch_klines")
def test_tp_strong_tier(mock_fetch):
    """量 2.2x → strong = 15"""
    mock_fetch.return_value = _base_klines(cur_vol=2200.0)
    pts, reason = score_tp("X", _cfg())
    assert pts == 15


@patch("tp_score._fetch_klines")
def test_tp_super_tier(mock_fetch):
    """量 2.6x + 盘整好（前 6 根 close 全是 100，range=0%）→ super = 20"""
    mock_fetch.return_value = _base_klines(cur_vol=2600.0)
    pts, reason = score_tp("X", _cfg())
    assert pts == 20
    assert "盘整" in reason


# ── 前高用 close 不用 high ──

@patch("tp_score._fetch_klines")
def test_tp_prev_high_uses_close_not_candle_high(mock_fetch):
    """
    lookback 某根 high=200 但 close=100 → prev_high_close=100。
    若错用 high，当前 close=104 将被误判为未突破（104 < 200*1.003）。
    正确用 close → 突破（104 > 100*1.003）。
    """
    lb = [_k(high=200.0, low=90.0, close=100.0, volume=1000)] + \
         [_k(high=101.0, low=99.0, close=100.0, volume=1000)] * 5
    klines = lb + [
        _k(high=99.8, low=99.0, close=99.5, volume=1000),   # prev
        _k(high=104.5, low=101.0, close=104.0, volume=2000), # current
    ]
    mock_fetch.return_value = klines
    pts, _ = score_tp("X", _cfg())
    assert pts > 0  # 如果用 high=200，这里 pts=0（错误）


# ── API 失败 fail-open ──

@patch("tp_score._fetch_klines", side_effect=Exception("timeout"))
def test_tp_api_error_fail_open(mock_fetch):
    """API 抛异常 → fail-open → 0"""
    pts, _ = score_tp("X", _cfg())
    assert pts == 0
