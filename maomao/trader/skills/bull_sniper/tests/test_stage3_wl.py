"""
Stage 3.2 WL 因子单测（v3.5-minimalist）
运行：python3 -m pytest tests/test_stage3_wl.py -v
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch
from analyzer import score_signal


def _cfg(**overrides):
    scoring = {
        # DN 全关（c1m 永远过不了 dn_reverse_guard=0 → guard 触发）
        "dn_reverse_guard": 0,   # c1m=0 < 0? No, 0 < 0 is False → guard off
        "dn_burst_1m": 999, "dn_score_1m": 0,
        "dn_burst_3m": 999, "dn_score_3m": 0,
        "dn_burst_5m": 999, "dn_score_5m": 0,
        "dn_burst_15m": 999, "dn_score_15m": 0,
        # WL
        "wl_score_tier1": 15,
        "wl_score_tier2": 12,
        "wl_score_tier3": 8,
        "wl_score_tier4": 3,
        # 其他因子关闭
        "funding_extreme_threshold": 9999,
        "announce_new_listing": 0, "announce_delist": 0,
    }
    scoring.update(overrides)
    return {"scoring": scoring}


def _md():
    return {"change_1m": 0, "change_3m": 0, "change_5m": 0, "change_15m": 0}


def _run(gain_pct, cfg=None):
    with patch("tp_score.score_tp", return_value=(0, "")), \
         patch("dd_score.score_dd", return_value=(0, "")):
        return score_signal("X", gain_pct=gain_pct, market_data=_md(), cfg=cfg or _cfg())


# ── 基准价正确性（gain_pct 由调用方传入，已从进池价算好） ──

def test_wl_no_vol_ratio_in_calc():
    """WL 计算不含 vol_ratio：score 只受 gain_pct 影响"""
    r1 = _run(7.0)
    r2 = _run(7.0, _cfg(some_vol_ratio_key=99))  # 额外的 vol 参数不影响结果
    assert r1["score"] == r2["score"] == 15


# ── 4 档命中 ──

def test_wl_tier1_5_to_10():
    """gain=7% ∈ [5,10) → WL=15"""
    r = _run(7.0)
    assert r["score"] == 15
    assert any("WL.早期" in k for k in r["breakdown"])

def test_wl_tier2_10_to_15():
    """gain=12% ∈ [10,15) → WL=12"""
    r = _run(12.0)
    assert r["score"] == 12
    assert any("WL.中期" in k for k in r["breakdown"])

def test_wl_tier3_15_to_25():
    """gain=20% ∈ [15,25) → WL=8"""
    r = _run(20.0)
    assert r["score"] == 8
    assert any("WL.强势" in k for k in r["breakdown"])

def test_wl_tier4_25_to_40():
    """gain=30% ∈ [25,40) → WL=3"""
    r = _run(30.0)
    assert r["score"] == 3
    assert any("WL.过热" in k for k in r["breakdown"])


# ── 边界外 → 0 ──

def test_wl_below_5():
    """gain=3% < 5 → WL=0（太早）"""
    r = _run(3.0)
    assert r["score"] == 0

def test_wl_exactly_40_is_zero():
    """gain=40%（等于上界）→ WL=0（过热）"""
    r = _run(40.0)
    assert r["score"] == 0

def test_wl_above_40():
    """gain=50% > 40 → WL=0（过热）"""
    r = _run(50.0)
    assert r["score"] == 0


# ── 档位边界值精确性 ──

def test_wl_boundary_5_exact():
    """gain=5.0（tier1 下边界，闭区间）→ WL=15"""
    r = _run(5.0)
    assert r["score"] == 15

def test_wl_boundary_10_exact():
    """gain=10.0（tier2 下边界，闭区间）→ WL=12"""
    r = _run(10.0)
    assert r["score"] == 12
