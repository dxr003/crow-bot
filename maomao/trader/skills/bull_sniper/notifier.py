"""
notifier.py — 做多阻击推送模块 v2.0
统一路由：玄玄(乌鸦私信) / 贝贝(群组) / 天天(震天响)
"""
import html as html_mod
import logging
import os
import sys
import time
import requests
import yaml
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from binance.um_futures import UMFutures

# 2026-04-27 Step 6-B: 走 api_hub 统一封装层（保留 requests 给 TG send 等）
if "/root/maomao" not in sys.path:
    sys.path.insert(0, "/root/maomao")
from trader.api_hub.binance import fapi as _fapi

logger = logging.getLogger("bull_notifier")

BB_BOT_TOKEN      = os.getenv("PUSH_BOT_TOKEN", "")
TT_BOT_TOKEN      = os.getenv("TT_BOT_TOKEN", "")
ADMIN_ID          = os.getenv("ADMIN_ID", "")
TT_CHAT_ID        = os.getenv("TT_CHAT_ID", "")
BROADCAST_CHAT_ID = os.getenv("BROADCAST_CHAT_ID", "")

# 关键 chat_id 缺失时启动期告警，避免 silent 走默认值推到错误目标
if not ADMIN_ID:
    ADMIN_ID = "509640925"
    logger.warning("[notifier] ADMIN_ID 未配置，使用默认 509640925")
if not BROADCAST_CHAT_ID:
    BROADCAST_CHAT_ID = "-1001150897644"
    logger.warning("[notifier] BROADCAST_CHAT_ID 未配置，使用默认 -1001150897644")

_cfg_cache = {}
_cfg_mtime = 0


def _load_notify_cfg():
    global _cfg_cache, _cfg_mtime
    cfg_path = Path(__file__).parent / "config.yaml"
    try:
        mt = cfg_path.stat().st_mtime
        if mt != _cfg_mtime:
            _cfg_cache = yaml.safe_load(cfg_path.read_text()).get("bull_sniper", {})
            _cfg_mtime = mt
    except Exception as e:
        logger.warning(f"[notifier] 配置加载失败: {e}")
    return _cfg_cache


def _mode_tail_line(cfg: dict | None = None) -> str:
    mode = str((cfg or _load_notify_cfg()).get("mode", "off")).lower()
    if mode == "auto":
        return "📡 当前 <b>自动开仓模式</b> · 信号触发即下单"
    if mode == "alert":
        return "🔔 当前 <b>告警模式</b> · 仅推送不下单"
    return "👁 当前 <b>纯观察模式</b> · 仅记录评分，不下单"


def _tg_send(token: str, chat_id: str, text: str):
    if not token or not chat_id:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"[notifier] 推送失败: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"[notifier] 推送异常: {e}")


def _send_admin(text: str):
    """贝贝→乌鸦私信（不走玄玄token，避免污染对话）"""
    _tg_send(BB_BOT_TOKEN, ADMIN_ID, text)


def _send_bb(text: str):
    """贝贝→群组（常开）"""
    _tg_send(BB_BOT_TOKEN, BROADCAST_CHAT_ID, text)


def _send_tt(text: str):
    """天天→震天响"""
    _tg_send(TT_BOT_TOKEN, TT_CHAT_ID, text)


# 推送并行线程池：最多 3 路（admin / tt / bb）
_push_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="notifier-push")


# event → 发送目标元组。bb 目标受 group_notify 开关控制。
# 新增事件在这里加一行即可，不用动 route() 逻辑。
_EVENT_TARGETS: dict[str, tuple[str, ...]] = {
    "open_success":  ("admin", "tt", "bb"),
    "open_fail":     ("admin", "tt", "bb"),
    "tp_activated":  ("admin", "tt", "bb"),
    "tp_closed":     ("admin", "tt", "bb"),
    "sl_closed":     ("admin", "tt", "bb"),
    "forced_close":  ("admin", "tt", "bb"),
    "order_fail":    ("admin", "tt", "bb"),
    "position_gone": ("admin", "tt"),
}


def route(event: str, text: str, text_group: str = None):
    """统一事件路由（v2.1 表驱动）。新增事件改 _EVENT_TARGETS 即可。
    bb 走 text_group（或 fallback 到 text），受 group_notify 开关门控。"""
    targets = _EVENT_TARGETS.get(event)
    if not targets:
        logger.warning(f"[notifier] 未知 event: {event}")
        return

    g = text_group or text
    group_on = _load_notify_cfg().get("group_notify", True)

    _SENDERS = {"admin": _send_admin, "bb": _send_bb, "tt": _send_tt}
    _MSGS = {"admin": text, "tt": text, "bb": g}

    jobs = []
    for t in targets:
        if t == "bb" and not group_on:
            continue
        jobs.append((_SENDERS[t], _MSGS[t]))

    # 3 路推送并行（fire-and-forget 风格，等全部完成再返回）
    futures = [_push_pool.submit(fn, msg) for fn, msg in jobs]
    for f in futures:
        try:
            f.result(timeout=15)
        except Exception as e:
            logger.warning(f"[notifier] 并行推送异常: {e}")


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


def _fmt_enter_time(ts: float) -> str:
    """timestamp → 北京时间 HH:MM"""
    from datetime import timezone, timedelta
    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%H:%M")


def _coin(symbol: str) -> str:
    return symbol.replace("USDT", "").replace("usdt", "")


def _fetch_prices(symbols: list) -> dict:
    """批量拉实时价格 {symbol: price}"""
    if not symbols:
        return {}
    try:
        all_px = _fapi.get_ticker_price()
        return {t["symbol"]: float(t["price"]) for t in all_px if t["symbol"] in symbols}
    except Exception as e:
        logger.warning(f"[notifier] 拉取价格失败: {e}")
        return {}


def _fetch_position_risk(symbols: list) -> dict:
    """从币安拉真实持仓数据 {symbol: {leverage, amt, mark, upnl, margin}}"""
    if not symbols:
        return {}
    try:
        key = os.getenv("BN2_API_KEY", "") or os.getenv("BINANCE_API_KEY", "")
        secret = os.getenv("BN2_API_SECRET", "") or os.getenv("BINANCE_API_SECRET", "")
        if not key or not secret:
            logger.warning("[notifier] BN2/BINANCE API key 未配置，跳过 position_risk 拉取")
            return {}
        client = UMFutures(key=key, secret=secret)
        data = client.get_position_risk()
        result = {}
        for p in data:
            sym = p.get("symbol", "")
            if sym in symbols and float(p.get("positionAmt", 0)) != 0:
                result[sym] = {
                    "leverage": int(p.get("leverage", 0)),
                    "amt": abs(float(p["positionAmt"])),
                    "mark": float(p.get("markPrice", 0)),
                    "upnl": float(p.get("unRealizedProfit", 0)),
                    "margin": float(p.get("isolatedWallet", 0)),
                }
        return result
    except Exception as e:
        logger.warning(f"[notifier] 拉取持仓风险失败: {e}")
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

def send_signal(signal: dict):
    """信号触发时立即推到群组"""
    symbol = signal["symbol"]
    entry_price = signal["entry_price"]
    cur_price = signal["cur_price"]
    gain_pct = signal["gain_pct"]
    vol = signal.get("volume_usdt", 0)
    drop_ath = signal.get("drop_from_ath", 0)
    elapsed = signal.get("elapsed_min", 0)
    score = signal.get("score", 0)
    breakdown = signal.get("breakdown", {})

    lines = [
        f"⚡️ <b>小刃 · 幻影信号预警 · {time.strftime('%m-%d %H:%M')}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🔥 <b>做多信号 — {_coin(symbol)}</b>",
        "",
        "<blockquote>",
        f"🪙 代币：<b>{_coin(symbol)}</b>",
        f"📈 进池价：<code>{_fmt_price(entry_price)}</code>",
        f"📈 当前涨幅：<code>+{gain_pct:.1f}%</code>（从进池算）",
        f"💹 24h成交量：<code>{_fmt_vol(vol)}</code>",
        f"📌 距历史高点：<code>-{drop_ath:.1f}%</code>",
        f"⏱ 观察时长：<code>{elapsed:.0f}分钟</code>",
        "",
        f"🔮 <i>小刃 AI · 幻影策略分析</i>",
        f"📊 综合评分：<b>{score}分</b>",
    ]
    for factor, pts in breakdown.items():
        lines.append(f"  · {factor}：<code>+{pts}</code>")
    lines += [
        "</blockquote>",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    lines.append(_mode_tail_line())

    msg = "\n".join(lines)
    _send_bb(msg)
    _send_tt(msg)


def send_pool_entry(pool_item: dict):
    """新币进入观察池——即时推简短通知"""
    symbol = pool_item["symbol"]
    change_5m = pool_item.get("change_5m", 0)
    vol = pool_item.get("volume_usdt", 0)
    drop_ath = pool_item.get("drop_from_ath", 0)

    text = (
        f"👁 <b>小刃 · 幻影 · 新目标进入观察</b>\n\n"
        f"<blockquote>"
        f"🪙 代币：<b>{_coin(symbol)}</b>\n"
        f"📈 5分钟涨幅：<code>+{change_5m:.1f}%</code>\n"
        f"💰 进池价：<code>{_fmt_price(pool_item.get('entry_price', 0))}</code>\n"
        f"💹 成交量：<code>{_fmt_vol(vol)}</code>\n"
        f"📌 距高点：<code>-{drop_ath:.1f}%</code>\n"
        f"⏱ 开始观察：{time.strftime('%H:%M:%S')}"
        f"</blockquote>\n\n"
        f"<i>观察中，等待 AI 决策推信号</i>"
    )
    _send_bb(text)


def send_status_card(state: dict):
    """
    整点状态卡片（XX:00推群组）
    包含观察池 + 信号记录（含analyzer结果） + 统计
    """
    now = time.time()
    watchpool = state.get("watchpool", {})
    signals = state.get("signals", [])
    positions = state.get("positions", {})

    # 一次性读配置（避免后面多次打开文件）
    cfg = _load_notify_cfg()
    max_hist = int(cfg.get("max_history_per_group", 10))

    # 并行拉取：position_risk（持仓账户）+ prices（持仓∪信号的全量 symbols）
    pos_symbols = list(positions.keys())
    sig_symbols = [s["symbol"] for s in signals] if signals else []
    all_price_symbols = list(set(pos_symbols) | set(sig_symbols))

    pos_risk: dict = {}
    live_prices: dict = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="notifier-card") as pool:
        f_risk = pool.submit(_fetch_position_risk, pos_symbols) if pos_symbols else None
        f_px = pool.submit(_fetch_prices, all_price_symbols) if all_price_symbols else None
        if f_risk:
            try:
                pos_risk = f_risk.result(timeout=12)
            except Exception as e:
                logger.warning(f"[notifier] 并行拉取持仓风险失败: {e}")
        if f_px:
            try:
                live_prices = f_px.result(timeout=12)
            except Exception as e:
                logger.warning(f"[notifier] 并行拉取价格失败: {e}")

    lines = [
        f"⚡️ <b>小刃 · 幻影信号预警 · {time.strftime('%m-%d %H:%M')}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    # ── 观察池 ──
    if watchpool:
        # 按进池时间排序
        sorted_wp = sorted(watchpool.items(), key=lambda x: x[1].get("entered_at", 0))
        lines.append(f"\n👁 <b>观察中（{len(sorted_wp)}个）</b>")
        lines.append("<blockquote>")
        for idx, (symbol, wp) in enumerate(sorted_wp, 1):
            entry_price = wp["entry_price"]
            cur_price = wp.get("cur_price", entry_price)
            peak_price = wp.get("peak_price", entry_price)
            gain_pct = (cur_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
            peak_pct = (peak_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
            sign = "+" if gain_pct >= 0 else ""
            enter_t = _fmt_enter_time(wp["entered_at"])
            wp_elapsed = _fmt_elapsed(wp["entered_at"])
            vol = wp.get("volume_usdt", 0)
            score = wp.get("last_score")
            score_tag = f"  评分<code>{score}</code>" if score is not None else ""
            frozen_tag = "❄️ " if wp.get("frozen") else ""
            sep = "\n" if idx > 1 else ""
            lines.append(
                f"{sep}<b>#{idx} {frozen_tag}{_coin(symbol)}</b>  "
                f"池内<code>{sign}{gain_pct:.1f}%</code>  "
                f"峰<code>+{peak_pct:.1f}%</code>  "
                f"<code>{_fmt_price(cur_price)}</code>\n"
                f"   {enter_t}发现 · 量<code>{_fmt_vol(vol)}</code> · "
                f"{wp_elapsed}{score_tag}"
            )
        lines.append("</blockquote>")
    else:
        lines.append("\n👁 <b>当前无观察目标</b>")

    # ── 持仓中 ──
    if positions:
        lines.append(f"\n💰 <b>持仓中（{len(positions)}个）</b>")
        lines.append("<blockquote>")
        for sym, pos in positions.items():
            entry_p = pos.get("entry_price", 0)
            info = pos_risk.get(sym, {})
            live_p = info.get("mark", 0) or live_prices.get(sym, 0)
            leverage = info.get("leverage", 5)
            upnl = info.get("upnl", 0)
            amt = info.get("amt", 0)
            if live_p > 0 and entry_p > 0:
                pnl = (live_p - entry_p) / entry_p * 100 * leverage
            else:
                pnl = 0
            peak = pos.get("peak_pnl_pct", 0)
            held_h = (now - pos.get("entry_time", now)) / 3600
            sl_id = pos.get("sl_algo_id", "")
            tp_id = pos.get("tp_algo_id", "")
            sl_tag = "SL✅" if sl_id and sl_id != "?" else "SL❌"
            tp_tag = "TP✅" if tp_id and tp_id != "?" else "TP❌"
            roll_count = pos.get("roll_count", 0)
            roll_tag = f"滚仓：已触发{roll_count}次" if roll_count > 0 else "滚仓：未触发"
            lines.append(
                f"<b>{_coin(sym)}</b> LONG {leverage}x\n"
                f"  入场<code>{_fmt_price(entry_p)}</code> → "
                f"现价<code>{_fmt_price(live_p)}</code>\n"
                f"  浮盈{_pnl_icon(pnl)}<b>{pnl:+.1f}%</b>  "
                f"峰<code>+{peak:.1f}%</code>  "
                f"持仓{held_h:.1f}h\n"
                f"  {roll_tag}\n"
                f"  {sl_tag}  {tp_tag}"
            )
        lines.append("</blockquote>")

    # ── 信号记录（含实时价格+浮盈） ──
    recent_signals = list(signals) if signals else []
    if recent_signals:
        # live_prices 已在入口并行拉取
        lines.append(f"\n✅ <b>已触发做多信号（{len(recent_signals)}个）</b>")
        for sig in reversed(recent_signals):
            action = sig.get("action", "")
            reason = sig.get("reason", "")
            score = sig.get("score")

            if action == "signal_fast":
                trigger_tag = f"⚡️ 快速通道 — {reason}"
            elif action == "signal_scored":
                trigger_tag = f"🔮 小刃 · 幻影捕获"
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
            lines.append("</blockquote>")
    else:
        lines.append("\n✅ <b>暂无触发信号</b>")

    # ── 历史结算（区分真实成交 vs 虚拟成绩） ──
    signal_history = state.get("signal_history", [])
    if signal_history:
        real_hist = [s for s in signal_history if not s.get("is_virtual")]
        virt_hist = [s for s in signal_history if s.get("is_virtual")]

        def _render_group(title: str, hist: list):
            if not hist:
                return
            success = [s for s in hist if s.get("status") == "success"]
            failed  = [s for s in hist if s.get("status") == "failed"]
            expired = [s for s in hist if s.get("status") == "expired"]
            lines.append(f"\n{title}（{len(hist)}个）")
            lines.append("<blockquote>")
            for label, icon, group in [
                ("成功", "✅", success),
                ("失败", "❌", failed),
                ("因故平仓", "⏰", expired),
            ]:
                if not group:
                    continue
                lines.append(f"{icon} <b>{label}（{len(group)}）</b>")
                shown = list(reversed(group))[:max_hist]
                for s in shown:
                    entry_p = s.get("entry_price", 0)
                    exit_p = s.get("exit_price", 0)
                    pnl = (exit_p - entry_p) / entry_p * 100 if entry_p > 0 and exit_p > 0 else 0
                    pnl_str = f"<b>{pnl:+.1f}%</b>" if exit_p > 0 else "<i>无出场价</i>"
                    lines.append(
                        f"  {_coin(s['symbol'])} "
                        f"入<code>{_fmt_price(entry_p)}</code> → "
                        f"出<code>{_fmt_price(exit_p)}</code> "
                        f"{pnl_str}"
                    )
                if len(group) > max_hist:
                    lines.append(f"  <i>…还有 {len(group) - max_hist} 条</i>")
            lines.append("</blockquote>")

        _render_group("📋 <b>真实交易结算</b>", real_hist)
        _render_group("📊 <b>虚拟成绩（未真实开仓）</b>", virt_hist)

    # ── 统计（与健康报告同步） ──
    pool_count = len(watchpool)
    pos_count = len(state.get("positions", {}))
    sig_count = len(signals)
    settled_count = len(state.get("signal_history", []))
    lines.append(
        f"\n🏆 <b>统计</b>  "
        f"观察池 {pool_count}  持仓 {pos_count}  "
        f"已触发信号 {sig_count}  结算 {settled_count}"
    )
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(_mode_tail_line(cfg))

    msg = "\n".join(lines)
    _send_bb(msg)
    _send_tt(msg)


def send_trade_report(signal: dict, buy_result: dict, analyze_result: dict):
    """
    成交详情报告 → 私信乌鸦
    完整展示：触发原因 → 分析过程 → 评分明细 → 入场参数 → 风控状态
    """
    sym = _coin(signal["symbol"])
    now_str = time.strftime("%m-%d %H:%M:%S")

    # ── 触发通道 ──
    action = signal.get("action", "")
    score = signal.get("score")

    channel = "🔮 小刃 · 幻影捕获" if action == "signal_scored" else f"📌 {action}"

    # ── 评分明细 ──
    analyze = analyze_result or {}
    breakdown = analyze.get("breakdown", {}) or signal.get("breakdown", {})
    analyze_lines = []
    if breakdown:
        analyze_lines.append("📋 评分明细：")
        for k, v in breakdown.items():
            analyze_lines.append(f"   {k}: {v:+d}")
        if score is not None:
            analyze_lines.append(f"   总分: {score}")
    analyze_block = "\n".join(analyze_lines) if analyze_lines else "无评分明细"

    # ── 执行结果 ──
    status = buy_result.get("status", "?")
    if status == "executed":
        exec_icon = "✅"
        sl_price = buy_result.get("sl_price", "?")
        order_id = buy_result.get("order_id", "?")
        sl_ok = buy_result.get("sl_algo_id") not in (None, "", "?")
        sl_tag = "✅已挂" if sl_ok else "⚠️挂载失败"
        tp_id = buy_result.get("tp_order_id", "")
        if isinstance(tp_id, str) and tp_id.startswith("layered:"):
            trailing_status = "✅已激活（分级追踪）"
        elif tp_id not in (None, "", "?"):
            trailing_status = f"✅已挂载（tp_id={tp_id}）"
        else:
            trailing_status = "⚠️挂载失败"
        exec_block = (
            f"订单ID: {order_id}\n"
            f"止损价: {sl_price}（保证金-{buy_result.get('sl_margin_pct', 50)}%）{sl_tag}\n"
            f"移动止盈: {trailing_status}"
        )
    elif status == "skipped":
        exec_icon = "⏭"
        exec_block = f"跳过：{buy_result.get('reason', '?')}"
    else:
        exec_icon = "❌"
        exec_block = f"失败：{buy_result.get('reason', '?')}"

    # ── 贝贝完整版（→乌鸦私信，含1234步骤） ──
    text_full = (
        f"{exec_icon} <b>小刃 · 幻影成交报告 · {sym}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now_str}\n\n"
        f"<b>1️⃣ 触发</b>\n"
        f"<blockquote>"
        f"{channel}\n"
        f"24h涨幅: +{signal.get('gain_pct', 0)}%\n"
        f"进池价: {_fmt_price(signal.get('entry_price', 0))}\n"
        f"触发价: {_fmt_price(signal.get('cur_price', 0))}\n"
        f"成交量: {_fmt_vol(signal.get('volume_usdt', 0))}\n"
        f"距ATH: -{signal.get('drop_from_ath', 0):.1f}%\n"
        f"观察时长: {signal.get('elapsed_min', 0):.0f}分钟"
        f"</blockquote>\n\n"
        f"<b>2️⃣ 评分明细</b>\n"
        f"<blockquote>{analyze_block}</blockquote>\n\n"
        f"<blockquote>{exec_block}</blockquote>"
    )

    # ── 精简版（→天天 + 群组贝贝，含评分明细供老大复盘）──
    text_simple = (
        f"{exec_icon} <b>小刃 · 幻影成交报告 · {sym}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now_str}\n\n"
        f"触发  {channel}\n"
        f"24h涨幅: +{signal.get('gain_pct', 0)}%  "
        f"触发价: {_fmt_price(signal.get('cur_price', 0))}\n\n"
        f"{analyze_block}\n\n"
        f"{exec_block}"
    )

    # 直接分发，不走route()
    # 2026-04-25 11:13 老大：成交报告只发贝贝私聊老大 + 天天，不发群
    _send_admin(text_full)        # 贝贝 bot → 老大私信（含评分明细）
    _send_tt(text_simple)         # 天天 bot → 震天响


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

    # 1) OI数据检查（v3.3: 改用币安历史API，抽检一次验证连通性）
    try:
        _oi_data = _fapi.get_open_interest_hist("BTCUSDT", period="15m", limit=1)
        if _oi_data:
            diag_lines.append("✅ OI：历史API连通正常")
        else:
            diag_lines.append("⚠️ OI：历史API返回异常")
            all_ok = False
    except Exception:
        diag_lines.append("⚠️ OI：历史API无法连接")
        all_ok = False

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

    filter_count = len(filter_log) if filter_log else 0

    # ── 6) 策略状态总览（2026-04-27 老大要求融合）──
    strategy_lines = []
    try:
        import yaml as _yaml
        # 幻影
        _phantom_cfg = _yaml.safe_load(open('/root/maomao/trader/skills/bull_sniper/config.yaml'))
        _pm = _phantom_cfg['bull_sniper'].get('mode', '?')
        _pm_str = repr(_pm) if not isinstance(_pm, str) else _pm
        _pm_label = '⛔off' if _pm == 'off' else '🟢auto' if _pm == 'auto' else '🟡alert' if _pm == 'alert' else f'⚠️{_pm_str}'
        _accs = _phantom_cfg['bull_sniper'].get('accounts', {})
        _on_accs = [a for a, c in _accs.items() if c.get('enabled')]
        _on_str = '/'.join(_on_accs) if _on_accs else '全关'
        strategy_lines.append(f"🔮 幻影：{_pm_label}  开:{_on_str}")
        # 潮汐
        _tide_cfg = _yaml.safe_load(open('/root/maomao/tide/config.yaml'))
        _tm = _tide_cfg.get('system', {}).get('mode', '?')
        _ms = _tide_cfg.get('mock_short_enabled')
        _tm_label = '⛔shadow' if _tm == 'shadow' else '🟢live' if _tm == 'live' else f'⚠️{_tm}'
        _ms_label = '关' if _ms is False else '开' if _ms is True else f'⚠️{_ms}'
        strategy_lines.append(f"🌊 潮汐：{_tm_label}  mock_short:{_ms_label}")
        # 链上 007
        _onc_cfg = _yaml.safe_load(open('/root/maomao/trader/skills/onchain_007/config.yaml'))
        _oc = _onc_cfg.get('onchain_007', {}).get('enabled')
        _oc_label = '🟢开' if _oc is True else '⛔关' if _oc is False else f'⚠️{_oc}'
        strategy_lines.append(f"🪐 链上007：{_oc_label}")
    except Exception as _e:
        strategy_lines.append(f"⚠️ 策略状态读取失败: {_e}")
    strategy_block = "\n".join(strategy_lines)

    # ── 7) 账户速览（2026-04-27 老大要求加，4 账户余额 + 持仓 + 浮盈）──
    acct_lines = []
    try:
        from trader.multi import executor as _exec
        for _ACC in ['币安1', '币安2', '币安3', '币安4']:
            try:
                _bal = _exec.get_balance('玄玄', _ACC)
                _f = _bal.get('futures', {})
                _total = _f.get('total', 0)
                _upnl = _f.get('upnl', 0)
                _pos = _exec.get_positions('玄玄', _ACC)
                if not _pos:
                    acct_lines.append(f"{_ACC}: 余{_total:.0f}U  浮盈{_upnl:+.1f}U  📭")
                else:
                    _items = []
                    for _p in _pos:
                        _amt = float(_p['positionAmt']); _sd = '多' if _amt > 0 else '空'
                        _u = float(_p['unRealizedProfit'])
                        _items.append(f"{_p['symbol'].replace('USDT','')}{_sd}{_u:+.0f}")
                    acct_lines.append(f"{_ACC}: 余{_total:.0f}U  " + " ".join(_items))
            except Exception as _e:
                acct_lines.append(f"{_ACC}: ❌ {str(_e)[:40]}")
    except Exception as _e:
        acct_lines.append(f"⚠️ 账户速览读取失败: {_e}")
    acct_block = "\n".join(acct_lines)

    text = (
        f"🔍 小刃 · 幻影系统自检 · {now_str}\n"
        f"━━━━━━━━━━━━━━\n"
        f"观察池：{len(watchpool)}  "
        f"持仓：{len(state.get('positions', {}))}  "
        f"已触发信号：{len(state.get('signals', []))}  "
        f"结算：{len(state.get('signal_history', []))}\n"
        f"━━ 🎛 策略状态 ━━\n"
        f"{strategy_block}\n"
        f"━━ 💰 账户速览 ━━\n"
        f"{acct_block}\n"
        f"━━ 🩺 健康检查 ━━\n"
        f"{diag_block}\n"
        f"⚠️ 近期过滤：{filter_count}个币未达标"
    )
    _send_admin(text)
