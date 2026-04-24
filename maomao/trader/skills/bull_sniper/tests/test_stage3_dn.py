"""
Stage 3.1 DN 因子单测（v3.5-minimalist）
运行：python3 -m pytest tests/test_stage3_dn.py -v
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch
from analyzer import score_signal


def _cfg(**dn_overrides):
    """只含 DN 相关 scoring 的最小 cfg，其余因子全关"""
    scoring = {
        "dn_reverse_guard": -1,
        "dn_burst_1m":  7,  "dn_score_1m":  55,
        "dn_burst_3m":  5,  "dn_score_3m":  42,
        "dn_burst_5m":  4,  "dn_score_5m":  33,
        "dn_burst_15m": 8,  "dn_score_15m": 22,
        "gain_5_10": 0, "gain_10_15": 0, "gain_15_25": 0, "gain_25_40": 0,
        "funding_extreme_threshold": 9999,
        "announce_new_listing": 0, "announce_delist": 0,
    }
    scoring.update(dn_overrides)
    return {"scoring": scoring}


def _md(c1m=0, c3m=0, c5m=0, c15m=0):
    return {"change_1m": c1m, "change_3m": c3m, "change_5m": c5m, "change_15m": c15m}


def _run(market_data, cfg):
    with patch("tp_score.score_tp", return_value=(0, "")), \
         patch("dd_score.score_dd", return_value=(0, "")):
        return score_signal("X", gain_pct=2, market_data=market_data, cfg=cfg)


# ── 反转守门（必须在档位判断之前） ──

def test_dn_reverse_guard_blocks_all():
    """1m=-2 < -1 → 守门触发，DN=0，即使其他档位全过门槛"""
    r = _run(_md(c1m=-2, c3m=6, c5m=5, c15m=9), _cfg())
    assert r["score"] == 0
    assert "DN.反转守门" in r["breakdown"]

def test_dn_reverse_guard_boundary():
    """1m=-0.5 ≥ -1 → 守门不触发，正常走档位（15m=9>8 → DN=22）"""
    r = _run(_md(c1m=-0.5, c15m=9), _cfg())
    assert r["score"] == 22
    assert "DN.反转守门" not in r["breakdown"]


# ── 四档互斥取最高 ──

def test_dn_tier_1m():
    """1m=8 > 7 → DN=55"""
    r = _run(_md(c1m=8), _cfg())
    assert r["score"] == 55

def test_dn_tier_3m():
    """1m=1（不过），3m=6 > 5 → DN=42"""
    r = _run(_md(c1m=1, c3m=6), _cfg())
    assert r["score"] == 42

def test_dn_tier_5m():
    """1m/3m 不过，5m=4.5 > 4 → DN=33"""
    r = _run(_md(c1m=1, c3m=1, c5m=4.5), _cfg())
    assert r["score"] == 33

def test_dn_tier_15m():
    """前三档不过，15m=9 > 8 → DN=22"""
    r = _run(_md(c1m=1, c3m=1, c5m=1, c15m=9), _cfg())
    assert r["score"] == 22

def test_dn_all_below_threshold():
    """全不过 → DN=0"""
    r = _run(_md(c1m=1, c3m=1, c5m=1, c15m=1), _cfg())
    assert r["score"] == 0

def test_dn_mutually_exclusive_1m_wins():
    """1m/3m/5m/15m 均过门槛 → 互斥取最高档（1m=55），不叠加"""
    r = _run(_md(c1m=8, c3m=6, c5m=5, c15m=9), _cfg())
    assert r["score"] == 55
    assert any("1m爆发" in k for k in r["breakdown"])
