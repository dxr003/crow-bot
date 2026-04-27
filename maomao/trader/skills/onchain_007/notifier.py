"""卡片渲染 + TG 群推送"""
from __future__ import annotations

import html
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logger = logging.getLogger("onchain_007.notifier")

BJ = timezone(timedelta(hours=8))

NET_TAG = {"solana": "SOL", "bsc": "BSC", "eth": "ETH"}

_BASE = Path(__file__).parent
_PUSH_LOG = _BASE / "data" / "push_log.jsonl"


def _fmt_money(n: float) -> str:
    if n >= 1e8:
        return f"${n/1e8:.2f}亿"
    if n >= 1e4:
        return f"${n/1e4:.0f}万"
    return f"${n:.0f}"


def _fmt_change(pct: float) -> str:
    return f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"


def _change_icon(pct: float, hot: float) -> str:
    """西方口径绿涨红跌：爆发💚（绿心）、普涨🍀、跌♦️"""
    if pct >= hot:
        return "💚"
    if pct >= 0:
        return "🍀"
    return "♦️"


def _fmt_age(hours: float) -> str:
    """上线时长：满 24h 显示天，否则显示小时"""
    if hours < 24:
        return f"{hours:.0f}h"
    days = int(hours // 24)
    rem_h = int(hours % 24)
    if rem_h == 0:
        return f"{days}天"
    return f"{days}天{rem_h}h"


def next_seq() -> int:
    if not _PUSH_LOG.exists():
        return 1
    try:
        last = _PUSH_LOG.read_text().strip().splitlines()[-1]
        return int(json.loads(last).get("seq", 0)) + 1
    except Exception:
        return 1


def record_push(seq: int, count: int, ok: bool):
    _PUSH_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"seq": seq, "count": count, "ok": ok, "ts": int(time.time())}
    with _PUSH_LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def render_card(pools: list[dict], cfg: dict, seq: int) -> str:
    now = datetime.now(BJ).strftime("%m-%d %H:%M")
    hot = float(cfg.get("hot_change_pct", 50))
    new_h = int(cfg.get("new_token_hours", 24))

    lines = [
        f"🔮 <b>链上 007 热榜信号 · #{seq}</b> · {now}",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    for i, p in enumerate(pools, 1):
        net = NET_TAG.get(p["network"], p["network"].upper())
        star = "⭐" * p.get("stars", 1)
        new_tag = "  🆕新币" if p["age_hours"] < new_h else ""
        # HTML escape symbol（防 < > & 等特殊字符破坏 parse_mode=HTML）
        sym_safe = html.escape(p["symbol"])
        lines.append(
            f"#{i} [<b>{net}</b>] <b>{sym_safe}</b>  💎 {_fmt_money(p['marketcap_usd'])}{new_tag}  {star}"
        )
        lines.append(
            f"{_change_icon(p['change_h1'], hot)} {_fmt_change(p['change_h1'])} 1h │ "
            f"{_change_icon(p['change_h24'], hot)} {_fmt_change(p['change_h24'])} 24h │ "
            f"量 {_fmt_money(p['volume_h24'])}"
        )
        lines.append(
            f"上线 {_fmt_age(p['age_hours'])} │ 流动性 {_fmt_money(p['liquidity_usd'])}"
        )
        lines.append("")

    if not pools:
        lines.append("🛡 当前无符合过滤条件的标的")
        lines.append("（市值 20-300万U / 流动性≥5万U / 24h量≥50万U / 上线≥6h）")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🤖 <i>小刃 AI 007 · 等待 AI 推触发信号</i>")
    return "\n".join(lines)


def push_to_group(text: str, cfg: dict) -> bool:
    """带 1 次重试 + HTML 解析失败时降级纯文本"""
    token = os.getenv(cfg["push_bot_token_env"], "")
    chat_id = cfg["push_targets"]["group"]
    if not token:
        logger.error(f"env {cfg['push_bot_token_env']} 未设置")
        return False

    def _send(payload: dict) -> tuple[bool, str]:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload, timeout=10,
            )
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {r.text[:200]}"
            return True, ""
        except Exception as e:
            return False, f"exception: {e}"

    # 1 试：HTML 模式
    ok, err = _send({"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    if ok:
        return True
    logger.warning(f"TG push HTML 失败: {err}")

    # 2 试：剥掉 HTML 标签发纯文本（兜底，群友看到无格式但保住信号）
    import re as _re
    plain = _re.sub(r"<[^>]+>", "", text)
    ok2, err2 = _send({"chat_id": chat_id, "text": plain})
    if ok2:
        logger.warning("TG push 降级纯文本成功")
        return True
    logger.error(f"TG push 双重失败: {err2}")
    return False
