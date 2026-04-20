"""strategy_router.py — 策略×账户状态控制台 v1.0（2026-04-19）

职责：
- 聚合各策略的当前状态（mode/enabled/目标账户）
- 提供"策略 × 账户"矩阵展示
- 给 bot 对话层调用（爸爸问"策略跑在哪"一句话出结果）

设计原则：
- 只读汇总，不改任何策略的运行配置
- 策略定义见 STRATEGIES 字典（未来加策略在这里注册即可）
- 不耦合策略内部实现，失败不崩溃（策略模块不存在就显示"未部署"）
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

import yaml

from trader.multi.registry import list_accounts

logger = logging.getLogger(__name__)


# ─────────────── mtime 文件缓存 ───────────────
# bot 高频"/状态"会重复打 yaml/json 解析；按 mtime 缓存解析结果，
# 文件未变直接复用，文件变了自动重读。
_FILE_CACHE: dict[str, tuple[float, Any, str | None]] = {}
_FILE_CACHE_LOCK = threading.Lock()


def _read_cached(path: Path, parse: Callable[[str], Any]) -> tuple[Any, str | None]:
    """返回 (data, err)。err 取值：None=正常 / "not_found" / 其它=解析异常字符串。"""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None, "not_found"
    cached = _FILE_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1], cached[2]
    with _FILE_CACHE_LOCK:
        cached = _FILE_CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1], cached[2]
        try:
            data = parse(path.read_text(encoding="utf-8"))
            err = None
        except Exception as e:
            data, err = None, str(e)
        _FILE_CACHE[key] = (mtime, data, err)
        return data, err


# ─────────────── 策略注册表 ───────────────
# 新策略上线 → 在这里加一条
STRATEGIES: dict[str, dict] = {
    "bull_sniper": {
        "name": "做多阻击",
        "config_path": "/root/maomao/trader/skills/bull_sniper/config.yaml",
        "config_section": "bull_sniper",
        "type": "auto",
    },
    "short_attack": {
        "name": "做空阻击",
        "config_path": None,   # 旧架构，没独立 YAML
        "runtime_file": "/root/short_attack/data/state.json",
        "type": "auto",
    },
    "manual_trade": {
        "name": "手动交易",
        "type": "manual",
        "note": "玄玄/大猫 通过对话下单，走 executor",
    },
    "trailing_v31": {
        "name": "移动止盈 v3.1",
        "runtime_file": "/root/maomao/data/trailing_state.json",
        "type": "auto",
        "note": "cron 每 5 分钟检查",
    },
    "rolling_v20": {
        "name": "滚仓 v2.0",
        "runtime_file": "/root/short_attack/data/roll_watch.json",
        "type": "semi",
        "note": "浮盈 50% 加仓，爸爸触发",
    },
}


# ─────────────── 读策略配置 ───────────────

def get_bull_sniper_status() -> dict:
    """读 bull_sniper config.yaml，返回 mode/accounts 绑定"""
    cfg, err = _read_cached(
        Path("/root/maomao/trader/skills/bull_sniper/config.yaml"),
        lambda s: yaml.safe_load(s) or {},
    )
    if err and err != "not_found":
        logger.warning(f"[strategy_router] bull_sniper config.yaml 解析失败: {err}")
    if not isinstance(cfg, dict):
        cfg = {}
    bs = cfg.get("bull_sniper", {}) if isinstance(cfg.get("bull_sniper"), dict) else {}
    accounts = bs.get("accounts", {}) if isinstance(bs.get("accounts"), dict) else {}
    scoring = bs.get("scoring", {}) if isinstance(bs.get("scoring"), dict) else {}
    return {
        "strategy": "bull_sniper",
        "name": "做多阻击",
        "enabled": bs.get("enabled", False),
        "mode": bs.get("mode", "off"),
        "accounts": {
            acc: {"enabled": meta.get("enabled", False) if isinstance(meta, dict) else False}
            for acc, meta in accounts.items()
        },
        "signal_threshold": scoring.get("signal_threshold", "?"),
    }


def get_short_attack_status() -> dict:
    """读 short_attack 状态"""
    st, err = _read_cached(Path("/root/short_attack/data/state.json"), json.loads)
    if err == "not_found":
        return {"strategy": "short_attack", "name": "做空阻击",
                "enabled": False, "note": "state.json 不存在"}
    if err:
        return {"strategy": "short_attack", "name": "做空阻击", "error": err}
    if not isinstance(st, dict):
        return {"strategy": "short_attack", "name": "做空阻击",
                "error": f"state.json 顶层非 dict (实际 {type(st).__name__})"}
    positions = st.get("positions") or {}
    monitoring = st.get("monitoring") or {}
    return {
        "strategy": "short_attack",
        "name": "做空阻击",
        "enabled": True,
        "mode": "alert",
        "accounts": {"币安1": {"enabled": True}},
        "active_positions": len(positions) if isinstance(positions, (dict, list)) else 0,
        "monitoring": len(monitoring) if isinstance(monitoring, (dict, list)) else 0,
    }


def get_all_status() -> list[dict]:
    """返回所有策略的当前状态"""
    out = []
    out.append(get_bull_sniper_status())
    out.append(get_short_attack_status())
    # 手动交易和 trailing/rolling 是能力而非持续策略，另做展示
    return out


# ─────────────── 矩阵展示 ───────────────

def format_matrix() -> str:
    """
    策略 × 账户 矩阵：
               币安1  币安2  币安3  币安4
    做多阻击     ⚪    ✅     ⚪    ⚪   [alert / 阈值28]
    做空阻击     ✅    ⚪     ⚪    ⚪   [alert]
    手动交易     ✅    ✅     ✅    ✅   [玄玄全权/天天除币安1]
    """
    accounts = [a["name"] for a in list_accounts(enabled_only=False)]
    statuses = get_all_status()

    lines = ["🎯 <b>策略 × 账户 矩阵</b>\n"]

    # 表头
    header = "策略" + " " * 8 + "  ".join(f"{a:<5s}" for a in accounts) + "  模式/备注"
    lines.append(f"<code>{header}</code>")

    for st in statuses:
        name = st.get("name", st["strategy"])
        row_icons = []
        bound = st.get("accounts", {})
        for acc in accounts:
            if bound.get(acc, {}).get("enabled"):
                row_icons.append("✅")
            elif acc in bound:
                row_icons.append("⚪")
            else:
                row_icons.append("—")

        mode = st.get("mode", "?")
        threshold = st.get("signal_threshold", "")
        note_bits = [mode]
        if threshold != "" and threshold != "?":
            note_bits.append(f"阈值{threshold}")
        if "error" in st:
            note_bits = [f"⚠️ {st['error'][:30]}"]
        note = " / ".join(note_bits)

        row = f"{name:<10s}" + "  ".join(f" {i}   " for i in row_icons) + f" [{note}]"
        lines.append(f"<code>{row}</code>")

    lines.append("")
    lines.append("<b>手动交易能力</b>")
    lines.append("  玄玄：全账户（executor 已就位）")
    lines.append("  大猫：全账户")
    lines.append("  天天：币安2/3/4（权限层已拦截币安1）")

    lines.append("")
    lines.append("<b>辅助模块（账户无关）</b>")
    lines.append("  🔒 移动止盈 v3.1 — 对所有开仓自动追踪")
    lines.append("  🔒 滚仓 v2.0 — 浮盈 50% 触发，爸爸手动发起")
    lines.append("  🔒 权限校验 — 每次执行前 require(role, action, account)")

    return "\n".join(lines)


if __name__ == "__main__":
    print(format_matrix())
    print()
    print("─── 原始状态 ───")
    import json
    print(json.dumps(get_all_status(), ensure_ascii=False, indent=2))
