"""guardian.py — 夜间守护 v1.0（2026-04-19）

每次运行做三项检查并输出报告：
  1. 4 个账户 API 可达性（启用的才查）
  2. systemd 服务状态（maomao/damao/tiantian/baobao/bull-sniper）
  3. bull_sniper 心跳（scanner.log 最近活动时间）

异常时推爸爸私信（通过贝贝 Bot PUSH_BOT_TOKEN）。
无异常时写 guardian_state.json 静默。

建议 cron：每 10 分钟跑一次
  */10 * * * * cd /root/maomao && python3 -m trader.multi.guardian >> /root/maomao/data/guardian.log 2>&1
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

from trader.multi.registry import list_accounts, get_futures_client
from trader.multi._atomic import atomic_write_json

logger = logging.getLogger(__name__)

load_dotenv("/root/maomao/.env")

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
    """查 systemd 服务状态"""
    results = []
    for svc in SERVICES:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5,
            )
            active = r.stdout.strip()
            results.append({"service": svc, "active": active, "ok": active == "active"})
        except Exception as e:
            results.append({"service": svc, "active": "?", "ok": False, "error": str(e)})
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
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


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
    report = {
        "ts": now.isoformat(),
        "accounts": check_accounts(),
        "services": check_services(),
        "bull_sniper_heartbeat": check_bull_sniper_heartbeat(),
        "anomalies": [],
    }

    # 收集异常
    for a in report["accounts"]:
        if not a["ok"]:
            report["anomalies"].append(
                f"账户异常 {a['account']}: {a.get('error', '?')}"
            )
    for s in report["services"]:
        if not s["ok"]:
            report["anomalies"].append(
                f"服务异常 {s['service']}: {s['active']}"
            )
    hb = report["bull_sniper_heartbeat"]
    if not hb["ok"]:
        if "reason" in hb:
            report["anomalies"].append(f"bull_sniper 心跳：{hb['reason']}")
        else:
            report["anomalies"].append(
                f"bull_sniper 心跳超时：{hb['age_sec']}秒未更新（阈值 {hb['threshold_sec']}秒）"
            )

    # 告警（去重）
    state = load_state()
    if report["anomalies"]:
        anomaly_key = "|".join(report["anomalies"])
        if should_alert(state, anomaly_key):
            lines = [f"⚠️ <b>夜间守护告警</b>  {now.strftime('%H:%M:%S')}"]
            for msg in report["anomalies"]:
                lines.append(f"  • {msg}")
            send_admin("\n".join(lines))
            mark_alert(state, anomaly_key)

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
