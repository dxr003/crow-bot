#!/usr/bin/env python3
"""Claude Code PostToolUse hook — AI 开发动作落盘。

乌鸦 2026-04-21 批：属铁律 7a 范畴。
Hook 由 Claude Code 运行时调用，stdin 传 JSON：
  { session_id, transcript_path, tool_name, tool_input, tool_response, ... }

落盘：/root/logs/dev/ai_actions.jsonl（每行一条，ISO8601+08）
失败静默退出，不影响宿主会话。
"""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BJ = timezone(timedelta(hours=8))
LOG = Path("/root/logs/dev/ai_actions.jsonl")


def main() -> None:
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return

    tool = ev.get("tool_name", "")
    inp = ev.get("tool_input") or {}
    resp = ev.get("tool_response") or {}

    entry = {
        "ts": datetime.now(BJ).isoformat(timespec="seconds"),
        "session": (ev.get("session_id") or "")[:8],
        "tool": tool,
    }

    if tool in ("Edit", "Write", "Read", "NotebookEdit"):
        entry["file"] = inp.get("file_path", "")
    elif tool == "Bash":
        cmd = (inp.get("command") or "").replace("\n", " ")
        entry["cmd"] = cmd[:300]
        if inp.get("description"):
            entry["desc"] = inp["description"][:120]
    elif tool in ("Grep", "Glob"):
        entry["pattern"] = inp.get("pattern", "")
        if inp.get("path"):
            entry["path"] = inp["path"]

    if isinstance(resp, dict) and resp.get("isError"):
        entry["error"] = True

    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
