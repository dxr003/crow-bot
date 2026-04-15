#!/usr/bin/env python3
"""
reject_tracker.py — 拒绝信号追踪器 v1.0

追踪所有退出观察池但未触发买入的币，监控6小时峰值走势。
用于验证三个策略问题：
  1. 进池门槛8%是否太高？（能不能降到5%）
  2. 信号触发区间10-20%是否太窄？（能不能降低门槛）
  3. 20%上限是否太保守？（超20%的币还能涨多少）

由 scanner 主循环调用，不额外请求API，搭便车用已有价格数据。
数据存储：data/reject_tracker.json
"""
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("reject_tracker")

DATA_FILE = Path(__file__).parent / "data" / "reject_tracker.json"
TRACK_HOURS = 6


def _load() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {"tracking": [], "history": []}


def _save(data: dict):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def record_exit(symbol: str, reason: str, score: int,
                exit_price: float, pool_entry_price: float,
                pool_gain_pct: float, breakdown: str = ""):
    """
    币退出观察池时调用。

    reason:
      - "score_low"   : 评分不够（在10-20%区间被分析但未达标）
      - "over_20"     : 涨幅超20%退出（不追高）
      - "under_5"     : 跌回5%以下退出（动力不足）
      - "veto"        : AI否决
      - "other"       : 其他

    pool_entry_price: 进池时的价格（首次触发8%时）
    exit_price: 退出时的当前价格
    pool_gain_pct: 退出时的24h涨幅
    """
    data = _load()

    # 避免重复记录（同一币种10分钟内不重复）
    now = time.time()
    for t in data["tracking"]:
        if t["symbol"] == symbol and now - t["exit_time"] < 600:
            return

    record = {
        "symbol": symbol,
        "reason": reason,
        "score": score,
        "breakdown": breakdown,
        "pool_entry_price": pool_entry_price,
        "exit_price": exit_price,
        "pool_gain_pct": round(pool_gain_pct, 1),
        "exit_time": now,
        "exit_time_str": time.strftime("%m-%d %H:%M"),
        "peak_price": exit_price,
        "peak_time": now,
        "last_price": exit_price,
        "samples": 0,
    }
    data["tracking"].append(record)
    _save(data)
    logger.info(
        f"[追踪] {symbol} 退出原因:{reason} 评分:{score} "
        f"退出价:{exit_price:.4f} 涨幅:{pool_gain_pct:+.1f}%"
    )


def update_peaks(live_prices: dict):
    """
    每轮扫描调用，用已有价格数据更新峰值。
    live_prices: {symbol: price} 全市场价格字典
    """
    data = _load()
    if not data["tracking"]:
        return

    now = time.time()
    changed = False
    still_tracking = []

    for rec in data["tracking"]:
        symbol = rec["symbol"]
        elapsed_h = (now - rec["exit_time"]) / 3600

        # 超过追踪时间 → 移入历史
        if elapsed_h >= TRACK_HOURS:
            rec["track_hours"] = round(elapsed_h, 1)
            _finalize(rec)
            data["history"].append(rec)
            if len(data["history"]) > 200:
                data["history"] = data["history"][-200:]
            changed = True
            logger.info(
                f"[追踪完成] {symbol} "
                f"退出涨幅:{rec['pool_gain_pct']:+.1f}% "
                f"峰值涨幅:{rec.get('peak_gain_from_exit', 0):+.1f}% "
                f"峰值涨幅(从进池):{rec.get('peak_gain_from_entry', 0):+.1f}%"
            )
            continue

        # 更新价格
        price = live_prices.get(symbol, 0)
        if price > 0:
            rec["last_price"] = price
            rec["samples"] += 1
            if price > rec["peak_price"]:
                rec["peak_price"] = price
                rec["peak_time"] = now
                changed = True

        still_tracking.append(rec)

    data["tracking"] = still_tracking
    if changed:
        _save(data)


def _finalize(rec: dict):
    """计算最终统计指标"""
    exit_p = rec["exit_price"]
    entry_p = rec["pool_entry_price"]
    peak_p = rec["peak_price"]
    last_p = rec["last_price"]

    # 从退出价算峰值涨幅
    if exit_p > 0:
        rec["peak_gain_from_exit"] = round((peak_p - exit_p) / exit_p * 100, 1)
        rec["final_gain_from_exit"] = round((last_p - exit_p) / exit_p * 100, 1)
    else:
        rec["peak_gain_from_exit"] = 0
        rec["final_gain_from_exit"] = 0

    # 从进池价算峰值涨幅（回答"如果在8%时就买"）
    if entry_p > 0:
        rec["peak_gain_from_entry"] = round((peak_p - entry_p) / entry_p * 100, 1)
        rec["final_gain_from_entry"] = round((last_p - entry_p) / entry_p * 100, 1)
    else:
        rec["peak_gain_from_entry"] = 0
        rec["final_gain_from_entry"] = 0

    # 判定：如果峰值涨幅>10%，算"错过机会"
    rec["missed"] = rec.get("peak_gain_from_exit", 0) >= 10


def get_daily_report(date_str: str = "") -> str:
    """生成日报中的拒绝回顾段落"""
    data = _load()
    history = data.get("history", [])
    if not history:
        return ""

    # 按退出原因分组
    by_reason = {
        "score_low": [],
        "over_20": [],
        "under_5": [],
        "veto": [],
        "other": [],
    }
    for rec in history[-50:]:
        r = rec.get("reason", "other")
        by_reason.setdefault(r, []).append(rec)

    lines = ["\n📋 <b>拒绝回顾（过去24h退出未买入）</b>"]

    reason_labels = {
        "score_low": "⚖️ 评分不够",
        "over_20": "🚀 超20%不追",
        "under_5": "📉 跌回退出",
        "veto": "🤖 AI否决",
    }

    total = 0
    missed = 0

    for reason, label in reason_labels.items():
        items = by_reason.get(reason, [])
        if not items:
            continue

        lines.append(f"\n<b>{label}（{len(items)}个）</b>")
        for rec in items[-10:]:
            coin = rec["symbol"].replace("USDT", "")
            score = rec.get("score", 0)
            exit_gain = rec.get("pool_gain_pct", 0)
            peak_from_exit = rec.get("peak_gain_from_exit", 0)
            peak_from_entry = rec.get("peak_gain_from_entry", 0)

            total += 1
            if rec.get("missed", False):
                missed += 1
                icon = "❌"
                verdict = "错过"
            else:
                icon = "✅"
                verdict = "正确"

            lines.append(
                f"  {icon} {coin}  评分:{score}  "
                f"退出+{exit_gain:.0f}%  "
                f"峰值+{peak_from_exit:+.1f}%  "
                f"<b>{verdict}</b>"
            )

    if total > 0:
        accuracy = (total - missed) / total * 100
        lines.append(
            f"\n📊 拒绝准确率: {accuracy:.0f}% "
            f"({total - missed}正确 / {missed}错过 / {total}总计)"
        )

    # 策略洞察
    over_20 = by_reason.get("over_20", [])
    if over_20:
        avg_peak = sum(r.get("peak_gain_from_exit", 0) for r in over_20) / len(over_20)
        big_miss = sum(1 for r in over_20 if r.get("peak_gain_from_exit", 0) >= 20)
        if big_miss > 0:
            lines.append(
                f"⚠️ 超20%退出的币中，{big_miss}个继续涨了20%+，"
                f"建议观察是否放宽上限"
            )

    score_low = by_reason.get("score_low", [])
    if score_low:
        big_miss_score = [r for r in score_low if r.get("peak_gain_from_exit", 0) >= 20]
        if big_miss_score:
            avg_score = sum(r.get("score", 0) for r in big_miss_score) / len(big_miss_score)
            lines.append(
                f"⚠️ {len(big_miss_score)}个评分不够的币涨了20%+，"
                f"平均评分{avg_score:.0f}，检查是否有因子遗漏"
            )

    return "\n".join(lines)


def get_tracking_status() -> str:
    """当前追踪中的币（给状态卡片用）"""
    data = _load()
    tracking = data.get("tracking", [])
    if not tracking:
        return ""

    now = time.time()
    lines = [f"\n🔍 <b>拒绝追踪中（{len(tracking)}个）</b>"]
    for rec in tracking:
        coin = rec["symbol"].replace("USDT", "")
        reason = {"score_low": "评分低", "over_20": ">20%", "under_5": "<5%",
                  "veto": "AI否决"}.get(rec["reason"], rec["reason"])
        elapsed = (now - rec["exit_time"]) / 3600
        peak_gain = 0
        if rec["exit_price"] > 0:
            peak_gain = (rec["peak_price"] - rec["exit_price"]) / rec["exit_price"] * 100
        lines.append(
            f"  {coin}  {reason}  {elapsed:.1f}h  峰值{peak_gain:+.1f}%"
        )
    return "\n".join(lines)
