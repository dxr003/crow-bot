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
    text = (
        f"🚨 <b>新目标入场 — {_coin(symbol)}</b>\n\n"
        f"📈 发现涨幅：+{mon['change_pct']}%\n"
        f"💰 发现价：{mon['price']}\n"
        f"💹 24h成交量：{_fmt_vol(mon['volume_usdt'])}\n"
        f"⏱ 监控开始：{time.strftime('%m-%d %H:%M')}\n\n"
        f"⚠️ 仅供参考，开空由爸爸决定"
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
        f"🔥 <b>做空信号触发 — {_coin(symbol)}</b>\n\n"
        f"📈 24h总涨幅：+{sig['total_rise']}%\n"
        f"📉 从最高价回撤：-{sig['pullback_pct']}%\n"
        f"💰 建议入场价：{sig['position_price']}\n"
        f"⚡ 建议强平价：{sig['liq_price']}（市价×120%）\n\n"
        f"📊 资金费率：{funding_str}\n"
        f"📌 OI变化：{oi_str}\n"
        f"💹 24h成交量：{_fmt_vol(vol)}\n\n"
        f"⚠️ 仅供参考，开空由爸爸决定"
    )
    _send(text)


def send_card(state: dict):
    """定时状态卡片——每60分钟"""
    now        = time.time()
    monitoring = state.get("monitoring", {})
    signals    = state.get("signals", {})
    exits      = state.get("exits", [])[:3]
    stats      = state.get("stats", {"success": 0, "failed": 0})

    lines = [f"🎯 <b>做空阻击 · {time.strftime('%m-%d %H:%M')}</b>"]
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    # 监控中
    lines.append(f"\n👁 <b>监控中（{len(monitoring)}个）</b>" if monitoring else "\n👁 <b>监控中：无</b>")
    if monitoring:
        rows = []
        for symbol, mon in monitoring.items():
            elapsed = _fmt_elapsed(mon["started_at"])
            # 总峰值 = 从24h前基准算到最高点（包含发现前涨幅 + 监控中涨幅）
            total_max = round(
                (1 + mon["entry_gain_pct"] / 100) * (mon["max_price"] / mon["price_at_entry"]) * 100 - 100, 1
            )
            vol_str = _fmt_vol(mon.get("volume_usdt", 0))
            rows.append(f"{_coin(symbol):<8} 监控+{mon['entry_gain_pct']}% 峰+{total_max}%  量{vol_str}  已监控{elapsed}")
        lines.append("<pre>" + "\n".join(rows) + "</pre>")

    # 持仓信号
    lines.append(f"\n🚨 <b>持仓信号（{len(signals)}个）</b>" if signals else "\n🚨 <b>持仓信号：无</b>")
    if signals:
        rows = []
        for symbol, sig in signals.items():
            elapsed  = _fmt_elapsed(sig["triggered_at"])
            rows.append(
                f"{_coin(symbol)}  入场+{sig['total_rise']}% · 回撤-{sig['pullback_pct']}%\n"
                f"  入场价:{sig['position_price']}  强平价:{sig['liq_price']}  {elapsed}"
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
                f"{icon} {_coin(ex['symbol']):<6} 最高+{ex['max_pct']}% · 出场+{ex['exit_pct']}% · {ex['reason']}  {ago}前"
            )
        lines.append("<pre>" + "\n".join(rows) + "</pre>")

    # 战绩 + 底部
    lines.append(f"\n🏆 <b>战绩</b>  ✅击中 {stats['success']}  ❌失败 {stats['failed']}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚠️ 仅供参考，开空由爸爸决定")

    _send("\n".join(lines))
