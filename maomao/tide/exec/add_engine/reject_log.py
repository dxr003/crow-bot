"""拒绝/通过日志，JSONL 追加写 + 关键事件 TG 告警。

告警策略（避免刷屏）：
  - fire 成功（live）          → 立即推 🔴
  - fire 成功（shadow）         → 立即推 📝
  - bridge/trigger 异常         → 立即推 ❌
  - 普通 guard 拒绝/no_fire    → 只进 jsonl，不推
  - 同一 (rule_id, kind) 在 ALERT_DEDUP_SEC 内只推一次
"""
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv("/root/maomao/.env")

LOG_FILE = Path("/root/maomao/tide/logs/add_engine.jsonl")
ALERT_STATE = Path("/root/maomao/tide/data/add_engine_alerts.json")
ALERT_DEDUP_SEC = 300   # 5 分钟去重窗口

# 复用大猫 bot（@maoju99bot）推运维告警，chat=乌鸦私信 509640925
_BOT_TOKEN = os.getenv("BOT_TOKEN") or ""
_CHAT_ID = os.getenv("ALERT_CHAT_ID") or "509640925"


def _write(kind: str, payload: dict):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "kind": kind, "ts": int(time.time())}
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _should_alert(rule_id: str, alert_kind: str) -> bool:
    """(rule_id, alert_kind) 去重窗口控制。"""
    try:
        ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
        s = json.loads(ALERT_STATE.read_text()) if ALERT_STATE.exists() else {}
    except Exception:
        s = {}
    key = f"{rule_id}::{alert_kind}"
    now = int(time.time())
    last = int(s.get(key, {}).get("last_ts", 0))
    if now - last < ALERT_DEDUP_SEC:
        return False
    s[key] = {"last_ts": now}
    try:
        ALERT_STATE.write_text(json.dumps(s, ensure_ascii=False))
    except Exception:
        pass
    return True


def _tg_push(msg: str):
    """推 TG。失败不抛异常，不阻塞主路径。"""
    if not _BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
            json={"chat_id": _CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=6,
        )
    except Exception:
        pass


def reject(rule_id: str, stage: str, reason: str, ctx_extra: dict | None = None):
    _write("reject", {"rule_id": rule_id, "stage": stage, "reason": reason,
                      "extra": ctx_extra or {}})

    # 只对"异常/失败"类 reject 推告警，普通 guard 拒绝（cooldown/liq_safety/quota）不推
    critical_markers = ("异常", "失败", "bridge")
    is_bridge_err = stage == "bridge"
    is_critical = is_bridge_err or any(m in reason for m in critical_markers)
    if is_critical and _should_alert(rule_id, f"reject:{stage}"):
        _tg_push(f"❌ [add_engine] {rule_id} {stage} 拒绝\n{reason}")


def fire(rule_id: str, price: float, margin_usd: float, side: str, account: str,
         shadow: bool, extra: dict | None = None):
    _write("fire", {"rule_id": rule_id, "price": price, "margin_usd": margin_usd,
                    "side": side, "account": account, "shadow": shadow,
                    "extra": extra or {}})

    if not _should_alert(rule_id, f"fire:{'shadow' if shadow else 'live'}"):
        return
    tag = "📝 [影子]" if shadow else "🔴 [实盘]"
    entry_type = (extra or {}).get("entry_type", "market")
    limit_price = (extra or {}).get("limit_price")
    extra_line = ""
    if entry_type == "limit" and limit_price:
        extra_line = f"\n挂单价 {limit_price:.4f}"
    _tg_push(
        f"{tag} add_engine fire\n"
        f"rule: {rule_id}\n"
        f"{side} {account} margin={margin_usd}U @~{price}\n"
        f"type: {entry_type}{extra_line}"
    )


def trigger_skip(rule_id: str, trigger_kind: str, reason: str):
    _write("trigger_skip", {"rule_id": rule_id, "trigger": trigger_kind, "reason": reason})

    # trigger 内部 "异常: ..." 才推（正常 no_fire 在 engine 里不走这条，只有真 raise 才到）
    if "异常" in reason and _should_alert(rule_id, f"trigger_err:{trigger_kind}"):
        _tg_push(f"❌ [add_engine] {rule_id} trigger={trigger_kind} 异常\n{reason}")
