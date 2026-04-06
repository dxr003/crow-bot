"""
Bot运行事件 + 系统快照日志
bot_log.json  — 上线/下线/异常事件，最近100条/7天
sys_log.json  — CPU/内存/磁盘/服务快照，最近288条（24小时×5分钟粒度）
"""
import json, time, shutil, subprocess
from datetime import datetime
from pathlib import Path

_DATA = Path("/root/maomao/data")
BOT_LOG_FILE = _DATA / "bot_log.json"
SYS_LOG_FILE = _DATA / "sys_log.json"

BOT_MAX_ITEMS = 100
BOT_MAX_DAYS  = 7
SYS_MAX_ITEMS = 288   # 24h × 12次/h（每5分钟一条）


# ── 内部工具 ──────────────────────────────────────────────

def _load(path: Path) -> list:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

def _save(path: Path, entries: list):
    _DATA.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

def _now_str() -> str:
    return datetime.now().strftime("%m-%d %H:%M:%S")

def _get_sys_stats() -> dict:
    # CPU 1分钟负载
    try:
        load1 = float(open("/proc/loadavg").read().split()[0])
    except Exception:
        load1 = -1.0

    # 内存（/proc/meminfo，单位kB）
    mem_pct = mem_used_mb = mem_total_mb = 0
    try:
        info = {}
        for line in open("/proc/meminfo"):
            parts = line.split(":")
            if len(parts) == 2:
                info[parts[0].strip()] = int(parts[1].strip().split()[0])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        if total:
            mem_pct      = round((total - avail) / total * 100, 1)
            mem_used_mb  = (total - avail) // 1024
            mem_total_mb = total // 1024
    except Exception:
        pass

    # 磁盘
    disk_pct = disk_free_gb = 0
    try:
        du = shutil.disk_usage("/")
        disk_pct     = round(du.used / du.total * 100, 1)
        disk_free_gb = round(du.free / (1024 ** 3), 1)
    except Exception:
        pass

    # 服务状态
    services = {}
    for svc in ["maomao", "damao", "baobao", "redis"]:
        try:
            r = subprocess.run(["systemctl", "is-active", svc],
                               capture_output=True, text=True, timeout=2)
            services[svc] = r.stdout.strip()
        except Exception:
            services[svc] = "unknown"

    return {
        "load1":        load1,
        "mem_pct":      mem_pct,
        "mem_used_mb":  mem_used_mb,
        "mem_total_mb": mem_total_mb,
        "disk_pct":     disk_pct,
        "disk_free_gb": disk_free_gb,
        "services":     services,
    }


# ── 写入 ──────────────────────────────────────────────────

def log_bot_event(event: str, detail: str = ""):
    """
    event: 'online' | 'offline' | 'error' | 'restart' | 自定义
    detail: 附加描述
    """
    entries = _load(BOT_LOG_FILE)
    now = time.time()
    entries.append({
        "ts":     int(now),
        "dt":     _now_str(),
        "event":  event,
        "detail": detail[:300] if detail else "",
    })
    # 保留策略：7天内 且 最近100条
    cutoff = now - BOT_MAX_DAYS * 86400
    entries = [e for e in entries if e.get("ts", 0) >= cutoff]
    entries = entries[-BOT_MAX_ITEMS:]
    _save(BOT_LOG_FILE, entries)

def log_sys_snapshot():
    """记录一条系统快照（由 heartbeat 每5分钟调用）"""
    stats = _get_sys_stats()
    entries = _load(SYS_LOG_FILE)
    entries.append({
        "ts":  int(time.time()),
        "dt":  _now_str(),
        **stats,
    })
    entries = entries[-SYS_MAX_ITEMS:]
    _save(SYS_LOG_FILE, entries)


# ── 查询 ──────────────────────────────────────────────────

def get_recent_bot_events(limit: int = 20) -> list:
    return list(reversed(_load(BOT_LOG_FILE)))[:limit]

def get_recent_sys_snapshots(limit: int = 6) -> list:
    return list(reversed(_load(SYS_LOG_FILE)))[:limit]


# ── 格式化 ────────────────────────────────────────────────

def format_bot_events_tg(entries: list) -> str:
    if not entries:
        return "📭 暂无运行事件"
    ICONS = {"online": "🟢", "offline": "🔴", "error": "💥", "restart": "🔄"}
    lines = []
    for e in entries:
        icon   = ICONS.get(e.get("event", ""), "ℹ️")
        detail = e.get("detail", "")
        lines.append(f"{icon} <b>{e['dt']}</b>  {e.get('event','')}"
                     + (f"\n  {detail}" if detail else ""))
    return "\n\n".join(lines)

def format_sys_snapshot_tg(entries: list) -> str:
    if not entries:
        return "📭 暂无系统快照"
    lines = ["<b>系统快照（最近几条）</b>\n"]
    for e in entries:
        svcs = e.get("services", {})
        svc_str = " ".join(
            f"{'✅' if v == 'active' else '❌'}{k}"
            for k, v in svcs.items()
        )
        lines.append(
            f"<b>{e['dt']}</b>\n"
            f"  CPU负载:{e.get('load1','?')}  "
            f"内存:{e.get('mem_pct','?')}%({e.get('mem_used_mb','?')}/{e.get('mem_total_mb','?')}MB)  "
            f"磁盘:{e.get('disk_pct','?')}%(剩{e.get('disk_free_gb','?')}G)\n"
            f"  {svc_str}"
        )
    return "\n\n".join(lines)
