"""账户注册中心 v1.0

职责：
- 从 accounts.yaml 加载账户清单（热重载）
- 管理 key/secret 凭证（按 env_file 懒加载）
- 提供权限检查（capabilities）
- 别名映射（alias → 正式账户名）

启停拔插：accounts.yaml 的 mtime 每次调用时检测，变了就重载。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).parent / "accounts.yaml"

_lock = threading.RLock()  # 可重入：get_futures_client 在 lock 内会再调 _load_config
_cache: dict[str, Any] = {
    "mtime": 0,
    "config": {},
    "futures_clients": {},   # UMFutures 实例
    "spot_clients": {},      # Spot 实例
    "env_loaded": set(),
}


# ══════════════════════════════════════════
# env_file 懒加载
# ══════════════════════════════════════════

def _load_env_file(path: str) -> None:
    """把 env_file 里的 key=value 合并到 os.environ（只加载一次）"""
    if path in _cache["env_loaded"]:
        return
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"账户凭证文件不存在: {path}")
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
    _cache["env_loaded"].add(path)


# ══════════════════════════════════════════
# 配置热重载
# ══════════════════════════════════════════

def _load_config() -> dict:
    """读取 accounts.yaml，检查 mtime 变化触发热重载"""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"accounts.yaml 不存在: {CONFIG_PATH}")
    mtime = CONFIG_PATH.stat().st_mtime
    with _lock:
        if mtime != _cache["mtime"]:
            _cache["config"] = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            _cache["mtime"] = mtime
            _cache["futures_clients"].clear()  # 配置变了，清空 client 缓存
            _cache["spot_clients"].clear()
        return _cache["config"]


def resolve_name(name: str) -> str:
    """把别名（main / lhb / 李红兵 等）解析成正式账户名（币安1 / 币安3）"""
    cfg = _load_config()
    accounts = cfg.get("accounts", {})
    if name in accounts:
        return name
    for official, meta in accounts.items():
        if name in (meta.get("alias") or []):
            return official
    raise KeyError(f"未知账户: {name}")


_resolve_name = resolve_name  # 旧私有名向后兼容（trailing/rolling 封板仍在用）


# ══════════════════════════════════════════
# 对外 API
# ══════════════════════════════════════════

def list_accounts(enabled_only: bool = False) -> list[dict]:
    """列出所有账户（含状态），enabled_only=True 只返回启用的"""
    cfg = _load_config()
    out = []
    for name, meta in (cfg.get("accounts") or {}).items():
        if enabled_only and not meta.get("enabled"):
            continue
        out.append({
            "name": name,
            "enabled": meta.get("enabled", False),
            "alias": meta.get("alias", []),
            "capabilities": meta.get("capabilities", {}),
            "note": meta.get("note", ""),
        })
    return out


def get_meta(name: str) -> dict:
    """获取账户元数据（含凭证路径、权限等）"""
    official = resolve_name(name)
    return _load_config()["accounts"][official] | {"name": official}


def is_enabled(name: str) -> bool:
    try:
        return bool(get_meta(name).get("enabled"))
    except KeyError:
        return False


def has_capability(name: str, cap: str) -> bool:
    """检查账户是否有某项能力（futures/spot/transfer）"""
    try:
        meta = get_meta(name)
    except KeyError:
        return False
    return bool(meta.get("enabled")) and bool((meta.get("capabilities") or {}).get(cap))


def require_capability(name: str, cap: str) -> dict:
    """权限检查，失败抛 PermissionError，成功返回 meta"""
    meta = get_meta(name)
    if not meta.get("enabled"):
        raise PermissionError(f"账户 {meta['name']} 未启用（enabled=false）")
    if not (meta.get("capabilities") or {}).get(cap):
        raise PermissionError(f"账户 {meta['name']} 无 {cap} 权限")
    return meta


def get_credentials(name: str) -> tuple[str, str]:
    """加载账户的 (api_key, api_secret)，按需加载 env_file"""
    meta = get_meta(name)
    env_file = meta.get("env_file")
    if env_file:
        _load_env_file(env_file)
    ak = os.environ.get(meta["key_env"])
    sk = os.environ.get(meta["secret_env"])
    if not ak or not sk:
        raise RuntimeError(
            f"账户 {meta['name']} 凭证缺失: key_env={meta['key_env']} secret_env={meta['secret_env']}"
        )
    return ak, sk


def get_futures_client(name: str):
    """获取账户的合约 UMFutures 客户端（线程安全，双检锁）"""
    from binance.um_futures import UMFutures
    official = resolve_name(name)
    cache = _cache["futures_clients"]
    if official in cache:
        return cache[official]
    with _lock:
        if official in cache:
            return cache[official]
        ak, sk = get_credentials(official)
        cache[official] = UMFutures(key=ak, secret=sk)
        return cache[official]


class _SpotCompat:
    """薄适配器：让 python-binance Client 兼容 binance-connector Spot 接口

    暴露 .account() / .funding_wallet() / .user_universal_transfer() 三个方法。
    """
    def __init__(self, client):
        self._c = client

    def account(self):
        return self._c.get_account()

    def funding_wallet(self):
        return self._c.funding_wallet()

    def user_universal_transfer(self, type: str, asset: str, amount, **kwargs):
        # python-binance 对应方法是 make_universal_transfer
        return self._c.make_universal_transfer(
            type=type, asset=asset, amount=str(amount), **kwargs
        )


def get_spot_client(name: str):
    """获取账户的现货 Spot 客户端（线程安全，双检锁）。

    优先用 binance-connector 的 Spot；没装时 fallback 到 python-binance 的 Client。
    """
    official = resolve_name(name)
    cache = _cache["spot_clients"]
    if official in cache:
        return cache[official]
    with _lock:
        if official in cache:
            return cache[official]
        ak, sk = get_credentials(official)
        try:
            from binance.spot import Spot
            client = Spot(api_key=ak, api_secret=sk)
        except ModuleNotFoundError:
            from binance.client import Client
            client = _SpotCompat(Client(ak, sk))
        cache[official] = client
        return client


# ══════════════════════════════════════════
# 格式化（给玄玄/群组展示用）
# ══════════════════════════════════════════

def format_accounts() -> str:
    """中文展示账户清单"""
    accounts = list_accounts()
    lines = [f"📋 账户清单（共 {len(accounts)} 个）"]
    for a in accounts:
        status = "🟢启用" if a["enabled"] else "⚪停用"
        caps = a["capabilities"]
        cap_tags = []
        if caps.get("futures"): cap_tags.append("合约")
        if caps.get("spot"): cap_tags.append("现货")
        if caps.get("transfer"): cap_tags.append("划转")
        cap_str = "/".join(cap_tags) if cap_tags else "无"
        alias = f"（别名: {', '.join(a['alias'])}）" if a["alias"] else ""
        lines.append(f"  {status} {a['name']}{alias}  [{cap_str}]")
        if a["note"]:
            lines.append(f"      {a['note']}")
    return "\n".join(lines)
