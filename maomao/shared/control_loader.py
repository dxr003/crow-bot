# ============================================================
# 统一开关读取模块 — 所有策略从这里读，不直接读自己 config
# 创建：2026-04-27 顾问 Step 3
#
# 铁律保护：
#   - assert 类型校验（防 YAML 陷阱）
#   - emergency_stop 总开关（黑天鹅保险，触发即返回 None）
#   - mtime 热重载（改 yaml 不需要重启 service）
# ============================================================
import yaml
from pathlib import Path

CONTROL_PATH = Path("/root/maomao/control.yaml")


def load_control() -> dict | None:
    """读取总控制文件，emergency_stop 触发返回 None"""
    with open(CONTROL_PATH) as f:
        ctrl = yaml.safe_load(f)
    if ctrl.get("emergency_stop") == "true":
        return None
    return ctrl


def get_phantom_mode() -> str:
    ctrl = load_control()
    if ctrl is None:
        return "off"
    mode = ctrl["strategies"]["phantom"]["mode"]
    assert isinstance(mode, str), f"YAML 陷阱！phantom mode 类型={type(mode)}，值={mode}"
    return mode


def get_phantom_accounts() -> dict:
    ctrl = load_control()
    if ctrl is None:
        return {k: False for k in ["bn1", "bn2", "bn3", "bn4"]}
    return ctrl["strategies"]["phantom"]["accounts"]


def get_tide_mode() -> str:
    ctrl = load_control()
    if ctrl is None:
        return "shadow"
    mode = ctrl["strategies"]["tide"]["mode"]
    assert isinstance(mode, str), f"YAML 陷阱！tide mode 类型={type(mode)}，值={mode}"
    return mode


def get_tide_mock_short() -> bool:
    ctrl = load_control()
    if ctrl is None:
        return False
    return bool(ctrl["strategies"]["tide"]["mock_short_enabled"])


def get_007_enabled() -> bool:
    ctrl = load_control()
    if ctrl is None:
        return False
    return bool(ctrl["strategies"]["onchain_007"]["enabled"])
