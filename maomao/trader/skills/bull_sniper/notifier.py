"""
notifier.py — 做多阻击推送模块
信号触发即时推到群组，不做整点卡片
"""
import os
import time
import requests

BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
BROADCAST_CHAT_ID = os.getenv("BROADCAST_CHAT_ID", "-1001150897644")


def _send(text: str):
    """推送到群组"""
    if not BOT_TOKEN:
        print(f"[notifier] 未配置BOT_TOKEN，跳过推送")
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
    if vol >= 1_000_000_000:
        return f"${vol/1_000_000_000:.1f}B"
    elif vol >= 1_000_000:
        return f"${vol/1_000_000:.0f}M"
    elif vol >= 1_000:
        return f"${vol/1_000:.0f}K"
    return f"${vol:.0f}"


def _fmt_elapsed(ts: float) -> str:
    elapsed = int(time.time() - ts)
    if elapsed < 60:
        return f"{elapsed}s"
    elif elapsed < 3600:
        return f"{elapsed // 60}m"
    return f"{elapsed // 3600}h{(elapsed % 3600) // 60}m"


def _coin(symbol: str) -> str:
    return symbol.replace("USDT", "").replace("usdt", "")


def _fmt_price(price: float) -> str:
    """智能格式化价格"""
    if price >= 100:
        return f"${price:.1f}"
    elif price >= 1:
        return f"${price:.2f}"
    elif price >= 0.01:
        return f"${price:.4f}"
    elif price >= 0.0001:
        return f"${price:.6f}"
    return f"${price:.8f}"


# ── 即时推送：信号触发 ──

def send_signal(signal: dict, watchpool_snapshot: list = None):
    """
    信号触发时立即推到群组
    signal: scanner产出的信号字典
    watchpool_snapshot: 当前观察池状态（可选，附在卡片下方）
    """
    symbol = signal["symbol"]
    entry_price = signal["entry_price"]
    cur_price = signal["cur_price"]
    gain_pct = signal["gain_pct"]
    vol = signal.get("volume_usdt", 0)
    drop_ath = signal.get("drop_from_ath", 0)
    elapsed = signal.get("elapsed_min", 0)

    lines = [
        f"⚡️ <b>小刃做多阻击信号预警 · {time.strftime('%m-%d %H:%M')}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🔥 <b>做多信号 — {_coin(symbol)}</b>",
        "",
        "<blockquote>",
        f"🪙 代币：<b>{_coin(symbol)}</b>",
        f"📈 进池价：<code>{_fmt_price(entry_price)}</code>",
        f"📈 当前涨幅：<code>+{gain_pct}%</code>（从进池算）",
        f"💹 24h成交量：<code>{_fmt_vol(vol)}</code>",
        f"📌 距历史高点：<code>-{drop_ath:.1f}%</code>",
        f"⏱ 观察时长：<code>{elapsed:.0f}分钟</code>",
        "</blockquote>",
        "",
        "<blockquote>",
        f"💰 参考入场：20U / 逐仓",
        f"⚠️ 仅供参考，开多由老大和社区兄弟们自行决定",
        "</blockquote>",
    ]

    # 附加观察池快照
    if watchpool_snapshot:
        lines.append("")
        lines.append(f"👁 <b>观察待做多信号（{len(watchpool_snapshot)}个）</b>")
        lines.append("<blockquote>")
        for wp in watchpool_snapshot:
            pool_gain = wp.get("gain_pct", 0)
            peak_gain = wp.get("peak_gain_pct", 0)
            wp_elapsed = _fmt_elapsed(wp["entered_at"])
            lines.append(
                f"<b>{_coin(wp['symbol'])}</b>  "
                f"进池<code>+{pool_gain:.1f}%</code>  "
                f"峰<code>+{peak_gain:.1f}%</code>  "
                f"现价<code>{_fmt_price(wp.get('cur_price', wp['entry_price']))}</code>  "
                f"量<code>{_fmt_vol(wp.get('volume_usdt', 0))}</code>  "
                f"{wp_elapsed}"
            )
        lines.append("</blockquote>")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("开多可以由玄玄自动执行，或由老大和社区兄弟们自行决定")

    _send("\n".join(lines))


def send_pool_entry(pool_item: dict):
    """新币进入观察池——即时推简短通知"""
    symbol = pool_item["symbol"]
    change_5m = pool_item.get("change_5m", 0)
    vol = pool_item.get("volume_usdt", 0)
    drop_ath = pool_item.get("drop_from_ath", 0)

    text = (
        f"👁 <b>小刃做多阻击 · 新目标进入观察</b>\n\n"
        f"<blockquote>"
        f"🪙 代币：<b>{_coin(symbol)}</b>\n"
        f"📈 5分钟涨幅：<code>+{change_5m:.1f}%</code>\n"
        f"💰 进池价：<code>{_fmt_price(pool_item.get('entry_price', 0))}</code>\n"
        f"💹 成交量：<code>{_fmt_vol(vol)}</code>\n"
        f"📌 距高点：<code>-{drop_ath:.1f}%</code>\n"
        f"⏱ 开始观察：{time.strftime('%H:%M:%S')}"
        f"</blockquote>\n\n"
        f"<i>正在观察，涨到10-18%时推信号</i>"
    )
    _send(text)


def send_status_card(state: dict):
    """
    状态总览卡片（可选，手动调用或定时触发）
    包含观察池 + 已记录信号 + 战绩
    """
    now = time.time()
    watchpool = state.get("watchpool", {})
    signals = state.get("signals", [])
    stats = state.get("stats", {})

    lines = [
        f"⚡️ <b>小刃做多阻击信号预警 · {time.strftime('%m-%d %H:%M')}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    # 观察池
    if watchpool:
        lines.append(f"\n👁 <b>观察待做多信号（{len(watchpool)}个）</b>")
        lines.append("<blockquote>")
        for symbol, wp in watchpool.items():
            entry_price = wp["entry_price"]
            cur_price = wp.get("cur_price", entry_price)
            peak_price = wp.get("peak_price", entry_price)
            gain_pct = (cur_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
            peak_pct = (peak_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
            wp_elapsed = _fmt_elapsed(wp["entered_at"])
            vol = wp.get("volume_usdt", 0)
            lines.append(
                f"<b>{_coin(symbol)}</b>  "
                f"进池<code>+{gain_pct:.1f}%</code>  "
                f"峰<code>+{peak_pct:.1f}%</code>  "
                f"现价<code>{_fmt_price(cur_price)}</code>  "
                f"量<code>{_fmt_vol(vol)}</code>  "
                f"{wp_elapsed}"
            )
        lines.append("</blockquote>")
    else:
        lines.append("\n👁 <b>当前无观察目标</b>")

    # 近期信号（最近10条）
    recent_signals = signals[-10:] if signals else []
    if recent_signals:
        lines.append(f"\n✅ <b>已记录做多信号（{len(recent_signals)}个）</b>")
        lines.append("<blockquote>")
        for sig in reversed(recent_signals):
            lines.append(
                f"<b>{_coin(sig['symbol'])}</b>  "
                f"入场<code>{_fmt_price(sig['entry_price'])}</code>→"
                f"触发<code>{_fmt_price(sig['cur_price'])}</code>  "
                f"<code>+{sig['gain_pct']}%</code>  "
                f"量<code>{_fmt_vol(sig.get('volume_usdt', 0))}</code>  "
                f"{sig.get('time', '')}"
            )
        lines.append("</blockquote>")
    else:
        lines.append("\n✅ <b>暂无信号记录</b>")

    # 战绩统计
    total_signals = stats.get("signals", 0)
    scans = stats.get("scans", 0)
    radar_hits = stats.get("radar_hits", 0)
    pool_entries = stats.get("pool_entries", 0)
    lines.append(
        f"\n🏆 <b>统计</b>  "
        f"扫描 {scans}  雷达 {radar_hits}  进池 {pool_entries}  信号 {total_signals}"
    )
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("开多可以由玄玄自动执行，或由老大和社区兄弟们自行决定")

    _send("\n".join(lines))
