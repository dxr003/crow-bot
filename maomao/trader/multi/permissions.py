"""角色权限校验 v1.0（2026-04-19）

职责：
- 加载 permissions.yaml（热重载，mtime 变化自动重读）
- check(role, action, account) → bool
- require(role, action, account) → 失败抛 PermissionError

典型用法（供 bot 执行入口调用）：
    from trader.multi.permissions import require
    require("天天", "trade", "币安1")   # 抛 PermissionError
    require("玄玄", "trade", "币安3")   # 通过
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from trader.multi.registry import resolve_name

CONFIG_PATH = Path(__file__).parent / "permissions.yaml"

_lock = threading.Lock()
_cache: dict[str, Any] = {"mtime": 0, "config": {}}

ACTION_META: dict[str, tuple[str, str]] = {
    "query": ("🔍", "查询"),
    "trade": ("💰", "执行"),
    "admin": ("⚙️", "配置"),
}
VALID_ACTIONS = set(ACTION_META)


def _load_config() -> dict:
    """热重载 permissions.yaml"""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"permissions.yaml 不存在: {CONFIG_PATH}")
    mtime = CONFIG_PATH.stat().st_mtime
    with _lock:
        if mtime != _cache["mtime"]:
            _cache["config"] = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            _cache["mtime"] = mtime
        return _cache["config"]


def _match(rules: list[str], account_official: str) -> bool:
    """
    匹配规则：
      - 显式 !账户 → 直接拒绝
      - "*"        → 通配允许（前提是没被 !拒绝）
      - 具名       → 精确匹配
    deny 优先 allow。
    """
    if not rules:
        return False
    deny = {r[1:] for r in rules if r.startswith("!")}
    if account_official in deny:
        return False
    if "*" in rules:
        return True
    return account_official in rules


def check(role: str, action: str, account: str) -> bool:
    """
    判断 role 是否有权限对 account 执行 action。
    account 可以是正式名（币安1）或别名（main/李红兵）。
    """
    if action not in VALID_ACTIONS:
        raise ValueError(f"非法 action: {action}（合法：{VALID_ACTIONS}）")

    cfg = _load_config()
    role_cfg = (cfg.get("roles") or {}).get(role)
    if role_cfg is None:
        return False   # 未登记的角色，默认拒绝

    # 账户名标准化（别名 → 正式名）
    try:
        official = resolve_name(account)
    except KeyError:
        return False   # 未知账户

    rules = role_cfg.get(action, [])
    return _match(rules, official)


def require(role: str, action: str, account: str) -> None:
    """权限断言，失败抛 PermissionError"""
    if not check(role, action, account):
        raise PermissionError(
            f"角色『{role}』无权对账户『{account}』执行『{action}』"
        )


def list_role_summary() -> str:
    """格式化展示：每个角色允许操作哪些账户（给 TG 看）"""
    from trader.multi.registry import list_accounts
    cfg = _load_config()
    accounts = [a["name"] for a in list_accounts()]

    lines = ["🔐 <b>角色权限清单</b>"]
    for role, rcfg in (cfg.get("roles") or {}).items():
        desc = rcfg.get("description", "")
        lines.append(f"\n<b>{role}</b> — {desc}")
        for act, (icon, name_cn) in ACTION_META.items():
            rules = rcfg.get(act, [])
            allowed = [a for a in accounts if _match(rules, a)]
            if not allowed:
                lines.append(f"  {icon} {name_cn}: —")
            else:
                lines.append(f"  {icon} {name_cn}: {'/'.join(allowed)}")
    return "\n".join(lines)


if __name__ == "__main__":
    # 自检矩阵
    tests = [
        ("大猫", "trade", "币安1", True),
        ("大猫", "admin", "币安4", True),
        ("玄玄", "trade", "币安1", True),
        ("玄玄", "trade", "币安3", True),
        ("玄玄", "admin", "币安2", True),
        ("天天", "query", "币安1", False),
        ("天天", "query", "币安2", True),
        ("天天", "query", "币安3", True),
        ("天天", "trade", "币安1", False),
        ("天天", "trade", "币安2", True),
        ("天天", "trade", "币安4", True),
        ("天天", "admin", "币安2", False),
        # 别名
        ("天天", "trade", "main", False),        # main = 币安1
        ("天天", "trade", "李红兵", True),        # = 币安3
        # 未登记角色
        ("路人", "query", "币安1", False),
    ]
    print("=== 权限自检 ===")
    ok = 0
    for role, act, acc, expect in tests:
        got = check(role, act, acc)
        mark = "✅" if got == expect else "❌"
        if got == expect:
            ok += 1
        print(f"  {mark} {role:3s} {act:5s} {acc:4s} → {got}  (期望 {expect})")
    print(f"\n{ok}/{len(tests)} 通过")
    print()
    print(list_role_summary())
