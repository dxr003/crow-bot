"""
notifier.py — 做多阻击推送模块
信号触发即时推到群组，健康报告私信乌鸦
"""
import html as html_mod
import os
import time
import requests
from datetime import datetime

BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
BROADCAST_CHAT_ID = os.getenv("BROADCAST_CHAT_ID", "-1001150897644")
ADMIN_ID          = os.getenv("ADMIN_ID", "509640925")


def _send(text: str, chat_id: str = None):
    """推送消息"""
    if not BOT_TOKEN:
        print(f"[notifier] 未配置BOT_TOKEN，跳过推送")
        return
    target = chat_id or BROADCAST_CHAT_ID
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    target,
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
    整点状态卡片（XX:00推群组）
    包含观察池 + 信号记录（含analyzer结果） + 统计
    """
    now = time.time()
    watchpool = state.get("watchpool", {})
    signals = state.get("signals", [])
    stats = state.get("stats", {})

    lines = [
        f"⚡️ <b>小刃做多阻击信号预警 · {time.strftime('%m-%d %H:%M')}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    # ── 观察池 ──
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

    # ── 信号记录（含 analyzer 结果） ──
    recent_signals = signals[-10:] if signals else []
    if recent_signals:
        lines.append(f"\n✅ <b>已触发做多信号（{len(recent_signals)}个）</b>")
        for sig in reversed(recent_signals):
            analyze = sig.get("analyze", {})
            action = analyze.get("action", "")
            reason = analyze.get("reason", "")
            score = analyze.get("score")
            breakdown = analyze.get("breakdown", {})
            news = analyze.get("news", {})
            news_titles = news.get("titles", [])[:2] if news else []

            # 触发类型标识
            if action == "signal_fast":
                trigger_tag = f"⚡ 快速通道 — {reason}"
            elif action == "signal_scored":
                trigger_tag = f"📊 评分通道 — {score}分"
            else:
                trigger_tag = f"📌 记录"

            lines.append("<blockquote>")
            lines.append(
                f"🪙 <b>{_coin(sig['symbol'])}</b>  "
                f"<code>+{sig['gain_pct']}%</code>  "
                f"{_fmt_vol(sig.get('volume_usdt', 0))}  "
                f"{sig.get('time', '')[5:]}"
            )
            lines.append(f"   {trigger_tag}")
            lines.append(
                f"   进池<code>{_fmt_price(sig['entry_price'])}</code> → "
                f"触发<code>{_fmt_price(sig['cur_price'])}</code>"
            )

            # 评分明细
            if breakdown:
                detail_parts = [f"{k}{v:+d}" for k, v in breakdown.items()]
                lines.append(f"   📋 {' / '.join(detail_parts)}")

            # 新闻摘要
            if news_titles:
                lines.append(f"   📰 {html_mod.escape(news_titles[0][:50])}")

            lines.append("</blockquote>")
    else:
        lines.append("\n✅ <b>暂无信号记录</b>")

    # ── 统计 ──
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


def send_health_report(state: dict, filter_log: list):
    """每小时健康播报 → 私信乌鸦，不进群"""
    from analyzer import _tavily_fail_count, _TAVILY_FAIL_THRESHOLD

    now_str = datetime.now().strftime("%m-%d %H:%M")
    stats = state.get("stats", {})

    # 新闻通道状态
    if _tavily_fail_count < _TAVILY_FAIL_THRESHOLD:
        news_status = "✅ Tavily"
    else:
        news_status = "⚠️ 已切Google RSS"

    # 过滤日志最近20条（转义HTML特殊字符）
    filter_lines = "\n".join(
        f"  {f['symbol']} {html_mod.escape(f['reason'])}"
        for f in filter_log[-20:]
    ) or "  无"

    text = (
        f"🔍 做多阻击 · {now_str}\n"
        f"━━━━━━━━━━━━━━\n"
        f"扫描次数：{stats.get('scans', 0)}次\n"
        f"雷达命中：{stats.get('radar_hits', 0)}个\n"
        f"进池：{stats.get('pool_entries', 0)}个\n"
        f"信号触发：{stats.get('signals', 0)}个\n"
        f"\n"
        f"新闻通道：{news_status}\n"
        f"下架公告：✅ 正常\n"
        f"观察池当前：{len(state.get('watchpool', {}))}个币\n"
        f"\n"
        f"⚠️ 近期过滤：\n{filter_lines}"
    )
    _send(text, chat_id=ADMIN_ID)
