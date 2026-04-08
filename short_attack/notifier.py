"""
notifier.py — 推送模块
负责三种卡片：新目标即时推、持仓信号即时推、定时状态卡片
"""
import os
import time
import requests

BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
BROADCAST_CHAT_ID = os.getenv("BROADCAST_CHAT_ID", "")


def _send(text: str):
    """推送到群组，失败打印错误不抛出"""
    if not BOT_TOKEN or not BROADCAST_CHAT_ID:
        print(f"[notifier] 未配置BOT_TOKEN或BROADCAST_CHAT_ID，跳过推送")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    BROADCAST_CHAT_ID,
                "text":       text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[notifier] 推送失败: {resp.text[:200]}")
    except Exception as e:
        print(f"[notifier] 推送异常: {e}")


def _fmt_vol(vol: float) -> str:
    """格式化成交量"""
    if vol >= 1_000_000_000:
        return f"${vol/1_000_000_000:.1f}B"
    elif vol >= 1_000_000:
        return f"${vol/1_000_000:.1f}M"
    elif vol >= 1_000:
        return f"${vol/1_000:.1f}K"
    return f"${vol:.0f}"

def _fmt_elapsed(ts: float) -> str:
    elapsed = int(time.time() - ts)
    if elapsed < 3600:
        return f"{elapsed // 60}m"
    return f"{elapsed // 3600}h"

def _coin(symbol: str) -> str:
    """去掉USDT后缀"""
    return symbol.replace("USDT", "").replace("usdt", "")


# ── 三种推送 ──────────────────────────────────────────────

def send_new_monitor(mon: dict):
    """新目标进入监控——立即推"""
    symbol = mon["symbol"]
    funding = mon.get("funding_rate", 0)
    funding_str = f"{funding:+.4f}%" if funding else "获取中"
    text = (
        f"🎯 <b>刃哥做空阻击</b>\n"
        f"🚨 <b>新目标进入监控等待买入时机 — {_coin(symbol)}</b>\n\n"
        f"📈 发现涨幅：+{mon['change_pct']}%\n"
        f"💰 发现价：{mon['price']}\n"
        f"💹 24h成交量：{_fmt_vol(mon['volume_usdt'])}\n"
        f"📊 资金费率：{funding_str}\n"
        f"⏱ 监控开始：{time.strftime('%m-%d %H:%M')}\n\n"
        f"开空可以由玄玄自动执行，或由老大和社区兄弟们自行决定"
    )
    _send(text)


def send_signal(sig: dict):
    """持仓信号触发——立即推醒目通知"""
    symbol = sig["symbol"]
    funding = sig.get("funding_rate", 0)
    oi_chg  = sig.get("oi_change_pct", 0)
    vol     = sig.get("volume_usdt", 0)
    funding_str = f"{funding:+.4f}%" if funding else "获取中"
    oi_str      = f"{oi_chg:+.1f}%" if oi_chg else "获取中"

    text = (
        f"🎯 <b>刃哥做空阻击</b>\n"
        f"🔥 <b>请立即执行做空 — {_coin(symbol)}</b>\n\n"
        f"📈 24h总涨幅：+{sig['total_rise']}%\n"
        f"📉 从最高价回撤：-{sig['pullback_pct']}%\n"
        f"💰 建议入场价：{sig['position_price']}\n"
        f"⚡ 建议强平价：{sig['liq_price']}\n\n"
        f"📊 资金费率：{funding_str}\n"
        f"📌 OI变化：{oi_str}\n"
        f"💹 24h成交量：{_fmt_vol(vol)}\n\n"
        f"开空可以由玄玄自动执行，或由老大和社区兄弟们自行决定"
    )
    _send(text)


def send_card(state: dict):
    """定时状态卡片——每60分钟"""
    now        = time.time()
    monitoring = state.get("monitoring", {})
    signals    = state.get("signals", {})
    exits      = state.get("exits", [])[:3]
    stats      = state.get("stats", {"success": 0, "failed": 0})

    lines = [f"🎯 <b>刃哥做空阻击 · {time.strftime('%m-%d %H:%M')}</b>"]
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    # 监控中
    lines.append(f"\n👁 <b>监控中（{len(monitoring)}个）</b>" if monitoring else "\n👁 <b>监控中：无</b>")
    if monitoring:
        rows = []
        for symbol, mon in monitoring.items():
            elapsed = _fmt_elapsed(mon["started_at"])
            entry_price = mon["price_at_entry"]
            base_price = entry_price / (1 + mon["entry_gain_pct"] / 100)
            # 峰值涨幅（从24h基准）
            total_max = round(
                (1 + mon["entry_gain_pct"] / 100) * (mon["max_price"] / entry_price) * 100 - 100, 1
            )
            # 实时涨幅（从24h基准）
            cur_price = mon.get("cur_price", entry_price)
            cur_gain = round((cur_price / base_price - 1) * 100, 1)
            vol_str = _fmt_vol(mon.get("volume_usdt", 0))
            funding = mon.get("funding_rate", 0)
            funding_str = f"费率{funding:+.4f}%" if funding else "费率获取中"
            rows.append(f"{_coin(symbol):<8} 现价{cur_price} 涨+{cur_gain}% 峰+{total_max}%\n         量{vol_str}  {funding_str}  {elapsed}")
        lines.append("<pre>" + "\n".join(rows) + "</pre>")

    # 阻击信号 / 持仓中
    positions = {s: d for s, d in signals.items() if d.get("executed")}
    pending   = {s: d for s, d in signals.items() if not d.get("executed")}

    if pending:
        lines.append(f"\n🚨 <b>阻击信号（{len(pending)}个）</b>")
        rows = []
        for symbol, sig in pending.items():
            elapsed = _fmt_elapsed(sig["triggered_at"])
            cur_price = sig.get("cur_price", sig["position_price"])
            rows.append(
                f"{_coin(symbol)}  涨+{sig['total_rise']}% · 回撤-{sig['pullback_pct']}%\n"
                f"  建议入场:{sig['position_price']}  现价:{cur_price}  {elapsed}"
            )
        lines.append("<pre>" + "\n\n".join(rows) + "</pre>")
    else:
        lines.append("\n🚨 <b>阻击信号：无</b>")

    if positions:
        lines.append(f"\n💀 <b>持仓中（{len(positions)}个）</b>")
        rows = []
        for symbol, sig in positions.items():
            elapsed = _fmt_elapsed(sig["triggered_at"])
            cur_price = sig.get("cur_price", sig["position_price"])
            pnl_pct = round((sig["position_price"] - cur_price) / sig["position_price"] * 100, 1)
            pnl_icon = "🟢" if pnl_pct > 0 else "🔴"
            rows.append(
                f"{_coin(symbol)}  入场:{sig['position_price']}  现价:{cur_price}\n"
                f"  {pnl_icon}浮盈:{pnl_pct:+.1f}%  强平:{sig['liq_price']}  {elapsed}"
            )
        lines.append("<pre>" + "\n\n".join(rows) + "</pre>")

    # 近期退出
    if exits:
        lines.append("\n⏹ <b>近期退出</b>")
        rows = []
        for ex in exits:
            icon = "✅" if ex["reason"] == "阻击成功" else ("❌" if ex["reason"] == "阻击失败" else "↩️")
            ago  = _fmt_elapsed(ex["exited_at"])
            rows.append(
                f"{icon} {_coin(ex['symbol']):<6} {ex['reason']}  {ago}前"
            )
        lines.append("<pre>" + "\n".join(rows) + "</pre>")

    # 战绩 + 底部
    lines.append(f"\n🏆 <b>战绩</b>  ✅击中 {stats['success']}  ❌失败 {stats['failed']}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("开空可以由玄玄自动执行，或由老大和社区兄弟们自行决定")

    _send("\n".join(lines))
