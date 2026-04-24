"""
Stage 3.4 DD 因子单测（v3.5-minimalist）
运行：python3 -m pytest tests/test_stage3_dd.py -v
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch
from dd_score import score_dd


def _trade(price: float, qty: float, is_buyer_maker: bool) -> dict:
    return {"p": str(price), "q": str(qty), "m": is_buyer_maker}


def _trades(buy_usdt: float, sell_usdt: float, price: float = 1.0):
    """构造一组 trades，主动买 buy_usdt U，主动卖 sell_usdt U"""
    result = []
    if buy_usdt > 0:
        result.append(_trade(price, buy_usdt / price, False))   # isBuyerMaker=False → taker buy
    if sell_usdt > 0:
        result.append(_trade(price, sell_usdt / price, True))   # isBuyerMaker=True  → taker sell
    return result


# ── fail-open ──

@patch("dd_score._fetch_agg_trades", side_effect=Exception("timeout"))
def test_dd_api_fail_open(mock_fetch):
    """API 抛异常 → fail-open → 0"""
    pts, _ = score_dd("X", {})
    assert pts == 0


# ── 窗口无成交 ──

@patch("dd_score._fetch_agg_trades", return_value=[])
def test_dd_no_trades(mock_fetch):
    """窗口无成交 → 0"""
    pts, _ = score_dd("X", {})
    assert pts == 0


# ── 极端情况：主动卖为 0 ──

@patch("dd_score._fetch_agg_trades")
def test_dd_zero_sell_high_vol(mock_fetch):
    """主动卖 = 0，买入 ≥ 10 万 U → 15"""
    mock_fetch.return_value = [_trade(1.0, 100_000.0, False)]  # buy=100000U, sell=0
    pts, reason = score_dd("X", {})
    assert pts == 15
    assert "极端买方" in reason


@patch("dd_score._fetch_agg_trades")
def test_dd_zero_sell_low_vol(mock_fetch):
    """主动卖 = 0，买入 < 10 万 U → 0（低量误判防护）"""
    mock_fetch.return_value = [_trade(100.0, 10.0, False)]  # buy=1000U, sell=0
    pts, _ = score_dd("X", {})
    assert pts == 0


# ── 4 个档位 ──

@patch("dd_score._fetch_agg_trades")
def test_dd_tier_super(mock_fetch):
    """ratio = 2.5 → 15"""
    mock_fetch.return_value = _trades(buy_usdt=2500, sell_usdt=1000)
    pts, reason = score_dd("X", {})
    assert pts == 15
    assert "超强" in reason


@patch("dd_score._fetch_agg_trades")
def test_dd_tier_strong(mock_fetch):
    """ratio = 2.0 → 12"""
    mock_fetch.return_value = _trades(buy_usdt=2000, sell_usdt=1000)
    pts, reason = score_dd("X", {})
    assert pts == 12
    assert "强买压" in reason


@patch("dd_score._fetch_agg_trades")
def test_dd_tier_mid(mock_fetch):
    """ratio = 1.5 → 8"""
    mock_fetch.return_value = _trades(buy_usdt=1500, sell_usdt=1000)
    pts, reason = score_dd("X", {})
    assert pts == 8


@patch("dd_score._fetch_agg_trades")
def test_dd_tier_mild(mock_fetch):
    """ratio = 1.2 → 4"""
    mock_fetch.return_value = _trades(buy_usdt=1200, sell_usdt=1000)
    pts, reason = score_dd("X", {})
    assert pts == 4


@patch("dd_score._fetch_agg_trades")
def test_dd_tier_none(mock_fetch):
    """ratio = 1.1 < 1.2 → 0"""
    mock_fetch.return_value = _trades(buy_usdt=1100, sell_usdt=1000)
    pts, _ = score_dd("X", {})
    assert pts == 0


# ── 边界：恰好 2.5 命中最高档 ──

@patch("dd_score._fetch_agg_trades")
def test_dd_boundary_2500(mock_fetch):
    """ratio 恰好 2.5 → 15（≥ 2.5 档）"""
    mock_fetch.return_value = _trades(buy_usdt=2500, sell_usdt=1000)
    pts, _ = score_dd("X", {})
    assert pts == 15
