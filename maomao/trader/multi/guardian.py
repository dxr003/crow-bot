"""guardian.py — 夜间守护 v1.0

每次运行做三项检查并输出报告：
  1. 启用账户的 API 可达性
  2. systemd 服务状态（maomao/damao/tiantian/baobao/bull-sniper）
  3. bull_sniper 心跳（scanner.log 最近活动时间）

异常时推 ADMIN 私信（通过 PUSH_BOT_TOKEN），无异常时只写 guardian_state.json。
部署命令与 cron 约定见部署手册，不在此处维护。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

if "/root" not in sys.path:
    sys.path.insert(0, "/root")
from ledger import get_ledger, new_trace_id

from trader.multi.registry import list_accounts, get_futures_client
from trader.multi._atomic import atomic_write_json

logger = logging.getLogger(__name__)

# L0 系统运维账本：system/guardian.jsonl
_sys_ledger = get_ledger("system", "guardian")

_env_loaded = False


def _ensure_env() -> None:
    """懒加载 .env（ADMIN_ID / PUSH_BOT_TOKEN）——避免 import guardian 就做磁盘 I/O + 环境注入"""
    global _env_loaded
    if not _env_loaded:
        load_dotenv("/root/maomao/.env")
        _env_loaded = True

# ── 配置 ──
SERVICES = ["maomao", "damao", "tiantian", "baobao", "bull-sniper"]
BULL_SNIPER_LOG = Path("/root/maomao/trader/skills/bull_sniper/logs/scanner.log")
HEARTBEAT_MAX_AGE_SEC = 5 * 60   # scanner.log 5分钟没更新 → 告警
STATE_PATH = Path("/root/maomao/data/guardian_state.json")

TZ_BJ = timezone(timedelta(hours=8))


# ══════════════════════════════════════════
# 检查项
# ══════════════════════════════════════════

def _check_one_account(name: str) -> dict:
    t0 = time.time()
    try:
        c = get_futures_client(name)
        c.account()
        elapsed = (time.time() - t0) * 1000
        return {"account": name, "ok": True, "ms": round(elapsed)}
    except Exception as e:
        return {"account": name, "ok": False, "error": str(e)[:120]}


def check_accounts() -> list[dict]:
    """并行查启用账户 API 是否可达（K7：fan-out 必须 ThreadPoolExecutor）"""
    names = [a["name"] for a in list_accounts(enabled_only=True)]
    if not names:
        return []
    with ThreadPoolExecutor(max_workers=min(len(names), 4)) as ex:
        return list(ex.map(_check_one_account, names))


def check_services() -> list[dict]:
    """查 systemd 服务状态（一次 systemctl 拿全部，省 5 次 fork+exec）"""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", *SERVICES],
            capture_output=True, text=True, timeout=10,
        )
        # is-active 多参时按行返回，failed/inactive 时退出码非 0 但仍有 stdout
        lines = r.stdout.strip().splitlines()
    except Exception as e:
        return [{"service": svc, "active": "?", "ok": False, "error": str(e)}
                for svc in SERVICES]

    results = []
    for svc, line in zip(SERVICES, lines + [""] * (len(SERVICES) - len(lines))):
        active = line.strip() or "?"
        results.append({"service": svc, "active": active, "ok": active == "active"})
    return results


def check_bull_sniper_heartbeat() -> dict:
    """bull_sniper scanner.log 最近修改时间"""
    if not BULL_SNIPER_LOG.exists():
        return {"ok": False, "reason": "scanner.log 不存在"}
    age = time.time() - BULL_SNIPER_LOG.stat().st_mtime
    return {
        "ok": age < HEARTBEAT_MAX_AGE_SEC,
        "age_sec": round(age),
        "threshold_sec": HEARTBEAT_MAX_AGE_SEC,
        "last_mtime": datetime.fromtimestamp(
            BULL_SNIPER_LOG.stat().st_mtime, tz=TZ_BJ
        ).strftime("%H:%M:%S"),
    }


# ══════════════════════════════════════════
# 告警
# ══════════════════════════════════════════

def send_admin(text: str) -> bool:
    _ensure_env()
    token = os.getenv("PUSH_BOT_TOKEN", "")
    admin = os.getenv("ADMIN_ID", "509640925")
    if not token:
        logger.warning("[guardian] PUSH_BOT_TOKEN 未配置，告警无法推送")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": admin, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code != 200:
            logger.error(f"[guardian] 告警推送失败 HTTP {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        logger.error(f"[guardian] 告警推送异常: {e}")
        return False


# ══════════════════════════════════════════
# 去重（相同异常 30 分钟内只告警一次，防止刷屏）
# ══════════════════════════════════════════

SUPPRESS_SEC = 30 * 60


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        bak = STATE_PATH.with_suffix(STATE_PATH.suffix + f".bad.{int(time.time())}")
        try:
            STATE_PATH.rename(bak)
        except Exception:
            pass
        logger.error(f"[guardian] state 文件损坏，已备份到 {bak.name}: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    atomic_write_json(STATE_PATH, state)


def should_alert(state: dict, key: str) -> bool:
    last = state.get("last_alerts", {}).get(key, 0)
    return time.time() - last > SUPPRESS_SEC


def mark_alert(state: dict, key: str) -> None:
    state.setdefault("last_alerts", {})[key] = time.time()


# ══════════════════════════════════════════
# 主逻辑
# ══════════════════════════════════════════

def run() -> dict:
    now = datetime.now(TZ_BJ)
    tid = new_trace_id()
    report = {
        "ts": now.isoformat(),
        "accounts": check_accounts(),
        "services": check_services(),
        "bull_sniper_heartbeat": check_bull_sniper_heartbeat(),
        "anomalies": [],
    }

    # 收集异常（消息+稳定去重 key 分开，避免动态文本破坏限频）
    anomaly_keys: list[str] = []
    for a in report["accounts"]:
        if not a["ok"]:
            report["anomalies"].append(
                f"账户异常 {a['account']}: {a.get('error', '?')}"
            )
            anomaly_keys.append(f"acct:{a['account']}")
    for s in report["services"]:
        if not s["ok"]:
            report["anomalies"].append(
                f"服务异常 {s['service']}: {s['active']}"
            )
            anomaly_keys.append(f"svc:{s['service']}")
    hb = report["bull_sniper_heartbeat"]
    if not hb["ok"]:
        if "reason" in hb:
            report["anomalies"].append(f"bull_sniper 心跳：{hb['reason']}")
        else:
            report["anomalies"].append(
                f"bull_sniper 心跳超时：{hb['age_sec']}秒未更新（阈值 {hb['threshold_sec']}秒）"
            )
        anomaly_keys.append("hb:bull_sniper")

    # 每次巡检落一条 heartbeat 事件（有/无异常都记），方便回溯运维时间线
    _sys_ledger.event("heartbeat", {
        "accounts_ok": sum(1 for a in report["accounts"] if a["ok"]),
        "accounts_total": len(report["accounts"]),
        "services_ok": sum(1 for s in report["services"] if s["ok"]),
        "services_total": len(report["services"]),
        "bull_sniper_age_sec": hb.get("age_sec"),
        "anomaly_count": len(report["anomalies"]),
    }, trace_id=tid, level="WARNING" if report["anomalies"] else "INFO")

    # 告警（去重）
    state = load_state()
    if report["anomalies"]:
        anomaly_key = "|".join(sorted(anomaly_keys))
        if should_alert(state, anomaly_key):
            lines = [f"⚠️ <b>夜间守护告警</b>  {now.strftime('%H:%M:%S')}"]
            for msg in report["anomalies"]:
                lines.append(f"  • {msg}")
            alert_text = "\n".join(lines)
            if send_admin(alert_text):
                mark_alert(state, anomaly_key)
                _sys_ledger.event("alert_sent", {
                    "anomalies": report["anomalies"],
                    "key": anomaly_key,
                }, trace_id=tid, level="WARNING")
            else:
                _sys_ledger.event("alert_send_failed", {
                    "anomalies": report["anomalies"],
                    "key": anomaly_key,
                }, trace_id=tid, level="ERROR")

    state["last_run"] = report["ts"]
    state["last_report"] = report
    save_state(state)
    return report


if __name__ == "__main__":
    r = run()
    print(f"时间: {r['ts']}")
    print(f"\n账户 API:")
    for a in r["accounts"]:
        mark = "✅" if a["ok"] else "❌"
        extra = f"{a.get('ms', '?')}ms" if a["ok"] else a.get("error", "")
        print(f"  {mark} {a['account']:8s} {extra}")
    print(f"\n服务:")
    for s in r["services"]:
        mark = "✅" if s["ok"] else "❌"
        print(f"  {mark} {s['service']:15s} {s['active']}")
    hb = r["bull_sniper_heartbeat"]
    mark = "✅" if hb["ok"] else "❌"
    print(f"\nbull_sniper 心跳: {mark}  最近 {hb.get('last_mtime', '?')}（{hb.get('age_sec', '?')}秒前）")
    if r["anomalies"]:
        print(f"\n⚠️ 异常 {len(r['anomalies'])} 条:")
        for msg in r["anomalies"]:
            print(f"  • {msg}")
    else:
        print("\n✅ 全部正常")
