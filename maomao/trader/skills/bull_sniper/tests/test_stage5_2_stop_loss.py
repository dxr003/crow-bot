"""
Stage 5.2 基础止损分级单测
运行：python3 -m pytest tests/test_stage5_2_stop_loss.py -v
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from stop_loss_manager import (
    is_high_entry,
    compute_initial_sl_pct,
    compute_upgraded_sl_pct,
    sl_price_from_pct,
    should_upgrade,
    is_sl_triggered,
    upgrade_stop_loss,
)

_CFG = {
    "stop_loss_30min_pct": 3,
    "stop_loss_after_pct": 5,
    "stop_loss_high_entry_30min": 2,
    "stop_loss_high_entry_after": 3,
    "high_entry_threshold_24h": 30,
    "sl_upgrade_window_minutes": 30,
}


# ── 高位判断 ──

def test_high_entry_above_threshold():
    assert is_high_entry(31.0, _CFG) is True

def test_high_entry_at_threshold():
    """恰好 30 不算高位（>30 才算）"""
    assert is_high_entry(30.0, _CFG) is False

def test_standard_entry():
    assert is_high_entry(25.0, _CFG) is False


# ── 初始止损幅度 ──

def test_initial_standard():
    assert compute_initial_sl_pct(25.0, _CFG) == 3.0

def test_initial_high_entry():
    assert compute_initial_sl_pct(35.0, _CFG) == 2.0


# ── 升级后止损幅度 ──

def test_upgraded_standard():
    assert compute_upgraded_sl_pct(25.0, _CFG) == 5.0

def test_upgraded_high_entry():
    assert compute_upgraded_sl_pct(35.0, _CFG) == 3.0


# ── 止损价计算 ──

def test_sl_price_3pct():
    """开仓价 100，止损 3% → 97.0"""
    assert abs(sl_price_from_pct(100.0, 3.0) - 97.0) < 1e-6

def test_sl_price_5pct():
    """开仓价 100，止损 5% → 95.0"""
    assert abs(sl_price_from_pct(100.0, 5.0) - 95.0) < 1e-6


# ── 升级时机判断（8个边界点）──

def _pos(open_time, upgraded=False):
    return {"position_open_time": open_time, "sl_upgraded": upgraded, "status": "holding"}

def test_standard_29min_no_upgrade():
    """标准入场 29 分钟 → 不触发升级"""
    pos = _pos(time.time() - 29 * 60)
    assert should_upgrade(pos, _CFG) is False

def test_standard_31min_upgrade():
    """标准入场 31 分钟 → 触发升级"""
    pos = _pos(time.time() - 31 * 60)
    assert should_upgrade(pos, _CFG) is True

def test_high_entry_29min_no_upgrade():
    """高位入场 29 分钟 → 不触发升级"""
    pos = _pos(time.time() - 29 * 60)
    assert should_upgrade(pos, _CFG) is False

def test_high_entry_31min_upgrade():
    """高位入场 31 分钟 → 触发升级"""
    pos = _pos(time.time() - 31 * 60)
    assert should_upgrade(pos, _CFG) is True

def test_already_upgraded_no_retry():
    """已经升级过 → 不再触发"""
    pos = _pos(time.time() - 60 * 60, upgraded=True)
    assert should_upgrade(pos, _CFG) is False


# ── 触发价边界（用止损幅度+开仓价验证具体值）──

def test_standard_initial_trigger():
    """开仓价100，入场25%，29min内 → 止损3%，触发价97"""
    pct = compute_initial_sl_pct(25.0, _CFG)   # 3%
    price = sl_price_from_pct(100.0, pct)
    assert abs(price - 97.0) < 1e-6

def test_standard_upgraded_trigger():
    """开仓价100，入场25%，31min后 → 止损5%，触发价95"""
    pct = compute_upgraded_sl_pct(25.0, _CFG)  # 5%
    price = sl_price_from_pct(100.0, pct)
    assert abs(price - 95.0) < 1e-6

def test_high_entry_initial_trigger():
    """开仓价100，入场35%（高位），29min内 → 止损2%，触发价98"""
    pct = compute_initial_sl_pct(35.0, _CFG)   # 2%
    price = sl_price_from_pct(100.0, pct)
    assert abs(price - 98.0) < 1e-6

def test_high_entry_upgraded_trigger():
    """开仓价100，入场35%（高位），31min后 → 止损3%，触发价97"""
    pct = compute_upgraded_sl_pct(35.0, _CFG)  # 3%
    price = sl_price_from_pct(100.0, pct)
    assert abs(price - 97.0) < 1e-6


# ── 8 个时间+价格叠加边界（is_sl_triggered）──

def test_standard_29min_drop_2p9_not_triggered():
    """标准入场 29min内 止损3%，跌2.9%→97.1 > 97.0 → 不触发"""
    assert is_sl_triggered(97.1, 100.0, 3.0) is False

def test_standard_29min_drop_3p1_triggered():
    """标准入场 29min内 止损3%，跌3.1%→96.9 < 97.0 → 触发"""
    assert is_sl_triggered(96.9, 100.0, 3.0) is True

def test_standard_31min_drop_3p1_not_triggered():
    """标准入场 31min后 止损5%，跌3.1%→96.9 > 95.0 → 不触发"""
    assert is_sl_triggered(96.9, 100.0, 5.0) is False

def test_standard_31min_drop_5p1_triggered():
    """标准入场 31min后 止损5%，跌5.1%→94.9 < 95.0 → 触发"""
    assert is_sl_triggered(94.9, 100.0, 5.0) is True

def test_high_entry_29min_drop_1p9_not_triggered():
    """高位入场 29min内 止损2%，跌1.9%→98.1 > 98.0 → 不触发"""
    assert is_sl_triggered(98.1, 100.0, 2.0) is False

def test_high_entry_29min_drop_2p1_triggered():
    """高位入场 29min内 止损2%，跌2.1%→97.9 < 98.0 → 触发"""
    assert is_sl_triggered(97.9, 100.0, 2.0) is True

def test_high_entry_31min_drop_2p9_not_triggered():
    """高位入场 31min后 止损3%，跌2.9%→97.1 > 97.0 → 不触发"""
    assert is_sl_triggered(97.1, 100.0, 3.0) is False

def test_high_entry_31min_drop_3p1_triggered():
    """高位入场 31min后 止损3%，跌3.1%→96.9 < 97.0 → 触发"""
    assert is_sl_triggered(96.9, 100.0, 3.0) is True


# ── 升级两步动作：cancel + place 各调用一次 ──

def test_upgrade_calls_cancel_then_place():
    """31min升级时：_cancel_algo_order 调用1次，_place_algo_stop_market 调用1次"""
    from unittest.mock import patch, MagicMock

    pos = {
        "position_open_time": time.time() - 31 * 60,
        "sl_upgraded": False,
        "status": "holding",
        "position_open_price": 100.0,
        "entry_24h_change_pct": 25.0,
        "sl_algo_id": "OLD_ID_123",
    }

    with patch("stop_loss_manager._cancel_algo_order", return_value=True) as mock_cancel, \
         patch("stop_loss_manager._get_position_qty", return_value=1.0), \
         patch("stop_loss_manager._place_algo_stop_market", return_value="NEW_ID_456") as mock_place:

        result = upgrade_stop_loss("XYZUSDT", pos, _CFG, "key", "secret")

    assert result is True
    mock_cancel.assert_called_once_with("XYZUSDT", "OLD_ID_123", "key", "secret")
    mock_place.assert_called_once()
    assert pos["sl_upgraded"] is True
    assert pos["sl_algo_id"] == "NEW_ID_456"
