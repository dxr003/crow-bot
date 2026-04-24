"""
Stage 5 最小冒烟测试：模块 import + 关键函数可调用
验证：模块无 ImportError、无模块级崩溃、关键函数存在且可调

运行：python3 -m pytest tests/test_stage5_smoke.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── 1. stop_loss_manager import 不崩 ──

def test_stop_loss_manager_importable():
    import stop_loss_manager  # noqa: F401


def test_stop_loss_manager_functions_exist():
    from stop_loss_manager import (
        is_high_entry,
        compute_initial_sl_pct,
        compute_upgraded_sl_pct,
        sl_price_from_pct,
        is_sl_triggered,
        should_upgrade,
        upgrade_stop_loss,
        upgrade_all_positions,
    )
    for fn in [is_high_entry, compute_initial_sl_pct, compute_upgraded_sl_pct,
               sl_price_from_pct, is_sl_triggered, should_upgrade,
               upgrade_stop_loss, upgrade_all_positions]:
        assert callable(fn), f"{fn.__name__} 不可调用"


# ── 2. trail_manager import 不崩 ──

def test_trail_manager_importable():
    import trail_manager  # noqa: F401


def test_trail_manager_functions_exist():
    from trail_manager import (
        get_active_layer,
        is_trail_triggered,
        compute_float_pnl,
        check_all,
        trail_loop,
    )
    for fn in [get_active_layer, is_trail_triggered, compute_float_pnl,
               check_all, trail_loop]:
        assert callable(fn), f"{fn.__name__} 不可调用"


# ── 3. scanner 模块级代码不崩（不调 run()）──

def test_scanner_importable():
    """import scanner 会执行模块级初始化代码，不应崩溃"""
    import scanner  # noqa: F401


def test_scanner_run_is_callable():
    import scanner
    assert callable(scanner.run)


# ── 4. config.yaml 包含必要的 Stage 5 参数 ──

def test_config_has_stage5_keys():
    import yaml
    from pathlib import Path
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    bs = cfg.get("bull_sniper", cfg)  # 兼容顶层或嵌套

    required = [
        "stop_loss_30min_pct",
        "stop_loss_after_pct",
        "stop_loss_high_entry_30min",
        "stop_loss_high_entry_after",
        "sl_upgrade_window_minutes",
        "trail_layer1_activation_pct",
        "trail_layer1_pullback_pct",
        "trail_layer2_activation_pct",
        "trail_layer2_pullback_pct",
        "trail_poll_interval_sec",
    ]
    missing = [k for k in required if k not in bs]
    assert not missing, f"config.yaml 缺少 Stage 5 参数: {missing}"
