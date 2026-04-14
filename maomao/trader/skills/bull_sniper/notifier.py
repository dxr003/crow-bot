"""
notifier.py — 做多阻击推送模块
信号触发即时推到群组，健康报告私信乌鸦
"""
import html as html_mod
import os
import time
import requests
from datetime import datetime

BOT_TOKEN         = os.getenv("PUSH_BOT_TOKEN", "") or os.getenv("BOT_TOKEN", "")
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


def _fetch_prices(symbols: list) -> dict:
    """批量拉实时价格 {symbol: price}"""
    if not symbols:
        return {}
    try:
        resp = requests.get("https://fapi.binance.com/fapi/v1/ticker/price", timeout=8)
        resp.raise_for_status()
        return {t["symbol"]: float(t["price"]) for t in resp.json() if t["symbol"] in symbols}
    except Exception:
        return {}


def _pnl_icon(pct: float) -> str:
    if pct >= 50: return "🟣"
    if pct >= 20: return "🔴"
    if pct >= 5:  return "🟢"
    if pct >= 0:  return "⚪"
    return "🔻"


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
            gain_since = wp.get("gain_since_entry", 0)
            peak_gain = wp.get("peak_gain_pct", 0)
            wp_elapsed = _fmt_elapsed(wp["entered_at"])
            sign = "+" if gain_since >= 0 else ""
            lines.append(
                f"<b>{_coin(wp['symbol'])}</b>  "
                f"池内<code>{sign}{gain_since:.1f}%</code>  "
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

    # ── 持仓中 ──
    positions = state.get("positions", {})
    if positions:
        lines.append(f"\n💰 <b>持仓中（{len(positions)}个）</b>")
        lines.append("<blockquote>")
        pos_symbols = list(positions.keys())
        pos_prices = _fetch_prices(pos_symbols)
        for sym, pos in positions.items():
            entry_p = pos.get("entry_price", 0)
            live_p = pos_prices.get(sym, 0)
            leverage = 5
            if live_p > 0 and entry_p > 0:
                pnl = (live_p - entry_p) / entry_p * 100 * leverage
            else:
                pnl = 0
            peak = pos.get("peak_pnl_pct", 0)
            held_h = (now - pos.get("entry_time", now)) / 3600
            sl_id = pos.get("sl_algo_id", "")
            tp_id = pos.get("tp_algo_id", "")
            sl_tag = "SL✅" if sl_id and sl_id != "?" else "SL❌"
            if tp_id == "trailing_limit":
                tp_tag = "TP限价✅"
            elif tp_id and tp_id != "?":
                tp_tag = "TP原生✅"
            else:
                tp_tag = "TP❌"
            lines.append(
                f"<b>{_coin(sym)}</b> LONG {leverage}x  "
                f"入场<code>{_fmt_price(entry_p)}</code>  "
                f"{_pnl_icon(pnl)}<b>{pnl:+.1f}%</b>  "
                f"峰<code>+{peak:.1f}%</code>  "
                f"已持仓{held_h:.1f}h\n"
                f"   {sl_tag} {tp_tag}"
            )
        lines.append("</blockquote>")

    # ── 信号记录（含实时价格+浮盈） ──
    recent_signals = signals[-10:] if signals else []
    if recent_signals:
        # 批量拉实时价格
        sig_symbols = [s["symbol"] for s in recent_signals]
        live_prices = _fetch_prices(sig_symbols)

        lines.append(f"\n✅ <b>已触发做多信号（{len(recent_signals)}个）</b>")
        for sig in reversed(recent_signals):
            action = sig.get("action", "")
            reason = sig.get("reason", "")
            score = sig.get("score")

            if action == "signal_fast":
                trigger_tag = f"⚡️ 快速通道 — {reason}"
            elif action == "signal_scored":
                trigger_tag = f"📊 评分通道 — {score}分"
            else:
                trigger_tag = f"📌 {reason}" if reason else "📌 记录"

            # 实时价格 & 浮盈
            entry_p = sig.get("entry_price", 0)
            live_p = live_prices.get(sig["symbol"], 0)
            if live_p > 0 and entry_p > 0:
                pnl_pct = (live_p - entry_p) / entry_p * 100
                icon = _pnl_icon(pnl_pct)
                price_line = (
                    f"   入场 <code>{_fmt_price(entry_p)}</code> → "
                    f"现价 <code>{_fmt_price(live_p)}</code>  "
                    f"{icon}<b>{pnl_pct:+.1f}%</b>"
                )
            else:
                price_line = (
                    f"   入场 <code>{_fmt_price(entry_p)}</code> → "
                    f"触发 <code>{_fmt_price(sig.get('cur_price', 0))}</code>"
                )

            lines.append("<blockquote>")
            lines.append(
                f"🪙 <b>{_coin(sig['symbol'])}</b>  "
                f"{_fmt_vol(sig.get('volume_usdt', 0))}  "
                f"{sig.get('time', '')[5:]}"
            )
            lines.append(f"   {trigger_tag}")
            lines.append(price_line)

            ai_reason = sig.get("ai_reason", "")
            if ai_reason:
                lines.append(f"   🤖 {html_mod.escape(ai_reason[:60])}")

            lines.append("</blockquote>")
    else:
        lines.append("\n✅ <b>暂无信号记录</b>")

    # ── 历史结算 ──
    signal_history = state.get("signal_history", [])
    if signal_history:
        success = [s for s in signal_history if s.get("status") == "success"]
        failed  = [s for s in signal_history if s.get("status") == "failed"]
        expired = [s for s in signal_history if s.get("status") == "expired"]

        lines.append(f"\n📋 <b>信号结算（{len(signal_history)}个）</b>")
        lines.append("<blockquote>")
        for label, icon, group in [
            ("成功", "✅", success),
            ("失败", "❌", failed),
            ("过期", "⏰", expired),
        ]:
            if group:
                lines.append(f"{icon} <b>{label}（{len(group)}）</b>")
                for s in group[-5:]:
                    entry_p = s.get("entry_price", 0)
                    exit_p = s.get("exit_price", 0)
                    pnl = (exit_p - entry_p) / entry_p * 100 if entry_p > 0 else 0
                    lines.append(
                        f"  {_coin(s['symbol'])} "
                        f"入<code>{_fmt_price(entry_p)}</code> → "
                        f"出<code>{_fmt_price(exit_p)}</code> "
                        f"<b>{pnl:+.1f}%</b>"
                    )
        lines.append("</blockquote>")

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


def send_trade_report(signal: dict, buy_result: dict, analyze_result: dict):
    """
    成交详情报告 → 私信乌鸦
    完整展示：触发原因 → 分析过程 → 评分明细 → 入场参数 → 风控状态
    """
    sym = _coin(signal["symbol"])
    now_str = time.strftime("%m-%d %H:%M:%S")

    # ── 触发通道 ──
    action = signal.get("action", "")
    reason = signal.get("reason", "")
    ai_reason = signal.get("ai_reason", "")
    score = signal.get("score")

    if action == "signal_fast":
        channel = f"⚡ 快速通道（8-10%）"
        trigger = f"触发原因：{reason}"
    elif action == "signal_scored":
        channel = f"📊 评分通道（10-20%）"
        trigger = f"触发原因：{score}分 ≥ 30分阈值"
    else:
        channel = f"📌 {action}"
        trigger = f"触发原因：{reason}"

    # ── 分析详情 ──
    analyze_lines = []
    analyze = analyze_result or {}

    # 新闻情绪
    news = analyze.get("news", {})
    if news:
        sentiment = news.get("sentiment", "")
        news_reason = news.get("reason", "")
        titles = news.get("titles", [])
        if sentiment:
            analyze_lines.append(f"📰 新闻情绪：{sentiment}")
        if news_reason:
            analyze_lines.append(f"   判断：{html_mod.escape(news_reason[:80])}")
        for t in titles[:2]:
            analyze_lines.append(f"   · {html_mod.escape(t[:60])}")

    # 下架检测
    delist = analyze.get("delist")
    if delist:
        analyze_lines.append(f"🚫 下架检测：{html_mod.escape(str(delist)[:60])}")

    # AI最终决策
    ai_decision = analyze.get("ai_decision", "")
    ai_full_reason = analyze.get("ai_reason", "") or ai_reason
    if ai_decision:
        analyze_lines.append(f"🤖 AI决策：{ai_decision}")
    if ai_full_reason:
        analyze_lines.append(f"   理由：{html_mod.escape(ai_full_reason[:100])}")

    # BTC关联
    btc_1h = analyze.get("btc_1h")
    if btc_1h is not None:
        analyze_lines.append(f"₿ BTC 1h：{btc_1h:+.2f}%")

    # 评分明细
    breakdown = analyze.get("breakdown", {})
    if breakdown:
        analyze_lines.append(f"📋 评分明细：")
        for k, v in breakdown.items():
            analyze_lines.append(f"   {k}: {v:+d}")
        if score is not None:
            analyze_lines.append(f"   总分: {score}")

    analyze_block = "\n".join(analyze_lines) if analyze_lines else "无详细分析数据"

    # ── 市场数据 ──
    market = analyze.get("market_data", {})
    market_lines = []
    if market:
        if "oi_change_pct" in market:
            market_lines.append(f"OI变化: {market['oi_change_pct']:+.1f}%")
        if "long_short_ratio" in market:
            market_lines.append(f"多空比: {market['long_short_ratio']:.2f}")
        if "funding_rate" in market:
            market_lines.append(f"费率: {market['funding_rate']*100:.4f}%")
        if "volume_ratio" in market:
            market_lines.append(f"量比: {market['volume_ratio']:.1f}x")
    market_block = " / ".join(market_lines) if market_lines else "—"

    # ── 执行结果 ──
    status = buy_result.get("status", "?")
    if status == "executed":
        exec_icon = "✅"
        sl_price = buy_result.get("sl_price", "?")
        order_id = buy_result.get("order_id", "?")
        sl_ok = buy_result.get("sl_algo_id") not in (None, "", "?")
        sl_tag = "✅已挂" if sl_ok else "⚠️挂载失败"
        tp_id = buy_result.get("tp_order_id", "")
        if tp_id == "trailing_limit":
            trailing_status = "✅已注册（限价单 50%激活/40%回撤）"
        elif tp_id not in (None, "", "?"):
            trailing_status = "✅已挂载（币安原生 50%激活/10%回撤）"
        else:
            trailing_status = "⚠️挂载失败"

        exec_block = (
            f"订单ID: {order_id}\n"
            f"止损价: {sl_price}（保证金-30%）{sl_tag}\n"
            f"移动止盈: {trailing_status}"
        )
    elif status == "skipped":
        exec_icon = "⏭"
        exec_block = f"跳过：{buy_result.get('reason', '?')}"
    else:
        exec_icon = "❌"
        exec_block = f"失败：{buy_result.get('reason', '?')}"

    # ── 组装卡片 ──
    text = (
        f"{exec_icon} <b>做多阻击成交报告 · {sym}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now_str}\n\n"

        f"<b>1️⃣ 触发</b>\n"
        f"<blockquote>"
        f"{channel}\n"
        f"{trigger}\n"
        f"24h涨幅: +{signal.get('gain_pct', 0)}%\n"
        f"进池价: {_fmt_price(signal.get('entry_price', 0))}\n"
        f"触发价: {_fmt_price(signal.get('cur_price', 0))}\n"
        f"成交量: {_fmt_vol(signal.get('volume_usdt', 0))}\n"
        f"距ATH: -{signal.get('drop_from_ath', 0):.1f}%\n"
        f"观察时长: {signal.get('elapsed_min', 0):.0f}分钟"
        f"</blockquote>\n\n"

        f"<b>2️⃣ 分析过程</b>\n"
        f"<blockquote>"
        f"{analyze_block}"
        f"</blockquote>\n\n"

        f"<b>3️⃣ 市场数据</b>\n"
        f"<blockquote>"
        f"{market_block}"
        f"</blockquote>\n\n"

        f"<b>4️⃣ 执行结果</b>\n"
        f"<blockquote>"
        f"{exec_block}"
        f"</blockquote>\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    _send(text, chat_id=ADMIN_ID)
    _send(text, chat_id=BROADCAST_CHAT_ID)


def send_health_report(state: dict, filter_log: list):
    """每小时健康播报 → 私信乌鸦，不进群。含系统自检诊断"""
    _tavily_fail_count = 0
    _TAVILY_FAIL_THRESHOLD = 999

    now_str = datetime.now().strftime("%m-%d %H:%M")
    now_ts = time.time()
    stats = state.get("stats", {})
    watchpool = state.get("watchpool", {})

    # ── 系统自检 ──
    diag_lines = []
    all_ok = True

    # 1) OI数据检查（通过state传入的oi_cache快照）
    oi_cache = state.get("_oi_cache", {})
    if not oi_cache:
        diag_lines.append("⚠️ OI：缓存为空，尚未采集到数据")
        all_ok = False
    else:
        freshest = max(v["time"] for v in oi_cache.values())
        age_min = (now_ts - freshest) / 60
        if age_min > 10:
            diag_lines.append(f"⚠️ OI：最新数据 {age_min:.0f}分钟前，可能断连")
            all_ok = False
        else:
            diag_lines.append(f"✅ OI：{len(oi_cache)}币缓存，最新 {age_min:.0f}分钟前")

    # 2) 新闻通道
    if _tavily_fail_count < _TAVILY_FAIL_THRESHOLD:
        diag_lines.append("✅ 新闻：Tavily 正常")
    else:
        diag_lines.append("⚠️ 新闻：已切Google RSS备用")
        all_ok = False

    # 3) 扫描活跃度
    scans = stats.get("scans", 0)
    if scans == 0:
        diag_lines.append("⚠️ 扫描：本周期0次，可能卡住")
        all_ok = False
    else:
        diag_lines.append(f"✅ 扫描：{scans}次")

    # 4) 评分活跃度（池内币是否被评分过）
    scored_count = sum(1 for wp in watchpool.values() if wp.get("analyzed") or wp.get("last_analyze_time", 0) > 0)
    pool_count = len(watchpool)
    if pool_count > 0 and scored_count == 0:
        diag_lines.append(f"⚠️ 评分：池内{pool_count}币但0个被评分")
        all_ok = False
    elif pool_count > 0:
        diag_lines.append(f"✅ 评分：池内{pool_count}币，{scored_count}个已评分")
    else:
        diag_lines.append("✅ 评分：池空，待观察")

    # 5) 状态文件完整性
    from pathlib import Path
    state_file = Path(__file__).parent / "data" / "scanner_state.json"
    if state_file.exists():
        age_min = (now_ts - state_file.stat().st_mtime) / 60
        if age_min > 5:
            diag_lines.append(f"⚠️ 状态文件：{age_min:.0f}分钟未更新")
            all_ok = False
        else:
            diag_lines.append(f"✅ 状态文件：{age_min:.0f}分钟前更新")
    else:
        diag_lines.append("⚠️ 状态文件：不存在")
        all_ok = False

    health_icon = "✅" if all_ok else "⚠️"
    diag_block = "\n".join(diag_lines)

    # ── 过滤日志 ──
    filter_lines = "\n".join(
        f"  {f['symbol']} {html_mod.escape(f['reason'])}"
        for f in filter_log[-20:]
    ) or "  无"

    text = (
        f"🔍 做多阻击 · {now_str}\n"
        f"━━━━━━━━━━━━━━\n"
        f"扫描：{stats.get('scans', 0)}  "
        f"雷达：{stats.get('radar_hits', 0)}  "
        f"进池：{stats.get('pool_entries', 0)}  "
        f"信号：{stats.get('signals', 0)}\n"
        f"观察池：{pool_count}个币\n"
        f"\n"
        f"{health_icon} <b>系统自检</b>\n"
        f"{diag_block}\n"
        f"\n"
        f"⚠️ 近期过滤：\n{filter_lines}"
    )
    _send(text, chat_id=ADMIN_ID)
