"""
Stage 5.1 持仓上限单测
运行：python3 -m pytest tests/test_stage5_1_position_limit.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch
import buyer

_CFG_5  = {"max_concurrent_positions": 5,  "position_usd": 50, "default_leverage": 5,
            "min_available_balance": 150, "max_slippage_pct": 2}
_CFG_10 = {"max_concurrent_positions": 10, "position_usd": 50, "default_leverage": 5,
           "min_available_balance": 150, "max_slippage_pct": 2}

def _fake_positions(n):
    """返回 n 个虚假多头持仓（symbol 各异）"""
    return [{"symbol": f"COIN{i}USDT", "positionAmt": "10"} for i in range(n)]


def _run(n_positions, cfg=None):
    """模拟已有 n 个多头持仓时尝试开新仓"""
    cfg = cfg or _CFG_5
    with patch("buyer._get_long_positions", return_value=_fake_positions(n_positions)), \
         patch("buyer._get_balance", return_value={"available": 500.0}):
        return buyer._execute_auto("NEWUSDT", 1.0, {}, cfg)


# ── 上限边界 ──

def test_at_limit_skipped():
    """持仓 5 个（=上限）→ skipped"""
    r = _run(5)
    assert r["status"] == "skipped"
    assert "5" in r["reason"]

def test_over_limit_skipped():
    """持仓 6 个（>上限）→ skipped"""
    r = _run(6)
    assert r["status"] == "skipped"

def test_under_limit_proceeds():
    """持仓 4 个（<上限）→ 不因持仓数拒绝（进入后续检查）"""
    r = _run(4)
    # 持仓数通过，后续因余额/balance mock而可能 skipped，但 reason 不含"上限"
    assert "上限" not in r.get("reason", "")

def test_zero_positions_proceeds():
    """持仓 0 个 → 不因持仓数拒绝"""
    r = _run(0)
    assert "上限" not in r.get("reason", "")

# ── 配置可覆盖 ──

def test_config_override_10():
    """config 改为 10，持仓 5 个 → 不触发上限"""
    r = _run(5, cfg=_CFG_10)
    assert "上限" not in r.get("reason", "")

def test_config_override_10_at_limit():
    """config 改为 10，持仓 10 个 → 触发上限"""
    r = _run(10, cfg=_CFG_10)
    assert r["status"] == "skipped"
    assert "10" in r["reason"]

# ── 默认值 ──

def test_default_fallback_is_5():
    """cfg 里没有 max_concurrent_positions → fallback=5，持仓5触发"""
    cfg_no_key = {"position_usd": 50, "default_leverage": 5,
                  "min_available_balance": 150, "max_slippage_pct": 2}
    r = _run(5, cfg=cfg_no_key)
    assert r["status"] == "skipped"
