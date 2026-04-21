#!/usr/bin/env python3
"""ai_history — 一行查询 AI 开发/业务动作日志。

乌鸦 2026-04-21 要求：改代码前必查日志、改完对比业务日志。
不再记 jq 语法，用这个 CLI。

用法：
  ai_history file <路径片段> [Nd|Nh]    最近 N 天/小时该文件改动
  ai_history exec <账户|币种>   [Nd|Nh] 业务日志里的调用
  ai_history signal            [Nd|Nh] 信号触发 / 拒绝
  ai_history trace <trace_id>          一 id 串 dev+exec+signal+dialog

时间窗默认 2d。输出格式统一：时间戳 | 摘要
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJ = timezone(timedelta(hours=8))
DEV_LOG = Path("/root/logs/dev/ai_actions.jsonl")
LOG_ROOT = Path("/root/logs")


def parse_window(s: str) -> timedelta:
    m = re.match(r"^(\d+)([hd])$", s.lower())
    if not m:
        raise SystemExit(f"时间窗格式错误: {s!r}（应为 2d / 6h 形式）")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(hours=n) if unit == "h" else timedelta(days=n)


def iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for ln in f:
            try:
                yield json.loads(ln)
            except Exception:
                continue


def within(ts: str, cutoff: datetime) -> bool:
    try:
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=BJ)
        return t >= cutoff
    except Exception:
        return False


def cmd_file(args: list[str]) -> None:
    if not args:
        raise SystemExit("用法: ai_history file <路径片段> [2d]")
    needle = args[0]
    window = parse_window(args[1]) if len(args) > 1 else timedelta(days=2)
    cutoff = datetime.now(BJ) - window

    print(f"=== 文件 {needle!r} 最近 {window} 改动 ===")
    rows = []
    for obj in iter_jsonl(DEV_LOG):
        ts = obj.get("ts", "")
        if not within(ts, cutoff):
            continue
        fp = obj.get("file", "")
        if needle not in fp:
            continue
        tool = obj.get("tool", "")
        if tool in ("Read",):
            continue
        rows.append((ts, tool, fp))
    for ts, tool, fp in rows:
        print(f"  {ts[:19]}  {tool:6s}  {fp}")
    print(f"\n  共 {len(rows)} 次变更（已过滤 Read）")


def cmd_exec(args: list[str]) -> None:
    if not args:
        raise SystemExit("用法: ai_history exec <账户|币种> [2d]")
    needle = args[0]
    window = parse_window(args[1]) if len(args) > 1 else timedelta(days=2)
    cutoff = datetime.now(BJ) - window

    print(f"=== exec/orders.jsonl 匹配 {needle!r}（最近 {window}）===")
    rows = []
    for obj in iter_jsonl(LOG_ROOT / "exec" / "orders.jsonl"):
        ts = obj.get("ts", "")
        if not within(ts, cutoff):
            continue
        blob = json.dumps(obj, ensure_ascii=False)
        if needle not in blob:
            continue
        ev = obj.get("event_type", "")
        pl = obj.get("payload") or {}
        acc = pl.get("account", "")
        sym = pl.get("symbol", "") or obj.get("target", "")
        ok = pl.get("ok")
        tag = "✅" if ok else ("❌" if ok is False else "·")
        tid = obj.get("trace_id", "")[:8]
        rows.append((ts, tag, ev, acc, sym, tid))
    for ts, tag, ev, acc, sym, tid in rows:
        print(f"  {ts[:19]}  {tag} {ev:18s} [{acc}] {sym}  trace={tid}")
    print(f"\n  共 {len(rows)} 条")


def cmd_signal(args: list[str]) -> None:
    window = parse_window(args[0]) if args else timedelta(days=2)
    cutoff = datetime.now(BJ) - window
    signal_dir = LOG_ROOT / "signal"
    files = sorted(signal_dir.glob("*.jsonl")) if signal_dir.exists() else []

    print(f"=== signal/ 最近 {window} 信号 ===")
    rows = []
    for p in files:
        for obj in iter_jsonl(p):
            ts = obj.get("ts", "")
            if not within(ts, cutoff):
                continue
            ev = obj.get("event") or obj.get("event_type", "")
            pl = obj.get("payload") or {}
            sym = pl.get("symbol", "")
            reason = pl.get("reason", "")
            rows.append((ts, p.stem, ev, sym, reason))
    for ts, mod, ev, sym, reason in rows:
        print(f"  {ts[:19]}  [{mod}] {ev:18s} {sym:12s} {reason}")
    print(f"\n  共 {len(rows)} 条")


def cmd_trace(args: list[str]) -> None:
    if not args:
        raise SystemExit("用法: ai_history trace <trace_id>")
    tid = args[0]
    print(f"=== trace_id={tid} 全域串联 ===")
    rows = []
    for domain in ("dialog", "exec", "signal", "risk", "external", "trace", "system"):
        d = LOG_ROOT / domain
        if not d.exists():
            continue
        for p in d.glob("*.jsonl"):
            for obj in iter_jsonl(p):
                if obj.get("trace_id", "") != tid:
                    continue
                ev = obj.get("event_type") or obj.get("event", "") or ""
                target = obj.get("target") or (obj.get("payload") or {}).get("symbol", "") or ""
                result = obj.get("result", "") or ""
                rows.append((obj.get("ts", "") or "", domain, ev, target, result))
    rows.sort()
    for ts, dom, ev, tgt, res in rows:
        print(f"  {ts[:19]}  [{dom:8s}] {ev:20s} {tgt:12s} {res}")
    print(f"\n  共 {len(rows)} 条")


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        sys.exit(0)
    sub, rest = argv[0], argv[1:]
    dispatch = {
        "file": cmd_file, "exec": cmd_exec,
        "signal": cmd_signal, "trace": cmd_trace,
    }
    fn = dispatch.get(sub)
    if not fn:
        raise SystemExit(f"未知子命令: {sub}（可用: file/exec/signal/trace）")
    fn(rest)


if __name__ == "__main__":
    main()
