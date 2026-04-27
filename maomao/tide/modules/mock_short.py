"""潮汐镜像推送 · 实盘/模拟自动切换"""
import os
import logging
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv("/root/maomao/.env")

_BAOBAO_TOKEN   = os.getenv("PUSH_BOT_TOKEN", "")
_GROUP_CHAT     = "-1001150897644"

log = logging.getLogger("tide.mock_short")

BJ = timezone(timedelta(hours=8))
_PUSH_MINUTES = {5}   # 2026-04-25 老大：30分→60分一次，每小时 :05 推

_ACTION_LABEL = {
    "OPEN_SHORT":   "开空",
    "OPEN_LONG":    "开多",
    "REDUCE_70":    "减仓70%",
    "REDUCE_50":    "减仓50%",
    "REDUCE_30":    "减仓30%",
    "ADD_1X":       "加仓1x",
    "ADD_1_5X":     "加仓1.5x",
    "ADD_2X":       "加仓2x",
    "ADD_3X":       "加仓3x",
    "TAKE_PROFIT":  "止盈",
    "STOP_LOSS":    "止损",
    "TRAIL_STOP":   "移动止盈",
    "FORCE_FLAT":   "爆仓💀",
    "NO_ACTION":    "观望",
    "NO_NEW_ACTION":"持仓不动",
    "WAIT":         "等待",
}


def _send(token: str, chat_id: str, text: str):
    if not token:
        log.warning("_send skipped: empty token")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"_send {chat_id} HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"_send {chat_id} exception: {e}")


def _zh(code: str) -> str:
    return _ACTION_LABEL.get(code, code) if code else ""


def _read_recent_actions(account: str, symbol: str, n: int = 3):
    """从 /root/logs/exec/orders.jsonl 读最近 n 条该账户+币种的真实动作"""
    import json
    from pathlib import Path
    log_path = Path("/root/logs/exec/orders.jsonl")
    if not log_path.exists():
        return []
    LABELS = {
        "close_market":      "平仓",
        "place_stop_loss":   "挂止损",
        "place_take_profit": "挂止盈",
        "cancel_all":        "撤全单",
        "cancel_order":      "撤单",
    }
    try:
        # 倒序扫描，扫到 n 条就停（高频日志下 500 行可能只覆盖 1 条目标记录）
        lines = log_path.read_text().splitlines()
    except Exception:
        return []
    out: list[str] = []
    for ln in reversed(lines[-5000:]):
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        et = rec.get("event_type", "")
        pl = rec.get("payload") or {}
        if pl.get("account") != account or pl.get("symbol") != symbol:
            continue
        if not pl.get("ok", False):
            continue
        if et == "open_market":
            side = (pl.get("args") or {}).get("side", "")
            label = "开空" if side == "SELL" else ("开多" if side == "BUY" else "开仓")
        elif et in LABELS:
            label = LABELS[et]
        else:
            continue
        out.append(label)
        if len(out) >= n:
            break
    out.reverse()  # 时间正序
    return out


_TRADE_EVENTS = {
    "open_market", "close_market", "place_stop_loss",
    "place_take_profit", "cancel_all", "cancel_order",
}


def _check_new_exec_ts(account: str, symbol: str, last_ts: str):
    """扫 exec_log 最末 500 行，返回 (是否有新于 last_ts 的真实动作, 最新 ts)"""
    import json
    from pathlib import Path
    log_path = Path("/root/logs/exec/orders.jsonl")
    if not log_path.exists():
        return False, last_ts
    try:
        lines = log_path.read_text().splitlines()[-500:]
    except Exception:
        return False, last_ts
    latest = last_ts
    has_new = False
    for ln in lines:
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if rec.get("event_type", "") not in _TRADE_EVENTS:
            continue
        pl = rec.get("payload") or {}
        if pl.get("account") != account or pl.get("symbol") != symbol:
            continue
        if not pl.get("ok", False):
            continue
        ts = rec.get("ts") or ""
        if ts > last_ts:
            has_new = True
            if ts > latest:
                latest = ts
    return has_new, latest


def _read_live_short(role: str, account: str, symbol: str):
    """读币安实盘空单。存在则返回 dict(entry/leverage/liq/qty)，否则 None。"""
    try:
        import sys
        if "/root/maomao" not in sys.path:
            sys.path.insert(0, "/root/maomao")
        from trader.multi import executor
        positions = executor.get_positions(role, account)
    except Exception as e:
        log.warning(f"_read_live_short exception: {type(e).__name__}: {e}")
        return None
    for p in positions or []:
        if p.get("symbol") != symbol:
            continue
        amt = float(p.get("positionAmt") or 0)
        if amt >= 0:          # 只镜像空仓（amt<0）
            continue
        return {
            "entry":    float(p.get("entryPrice") or 0),
            "leverage": float(p.get("leverage") or 1),
            "liq":      float(p.get("liquidationPrice") or 0),
            "qty":      abs(amt),
        }
    return None


def push_mock_short(cfg: dict, price: float, zone: dict, state: dict, write_state_fn):
    """模拟空仓镜像卡片
    - 定点 :05 / :35 推送
    - 区段切换到真实动作（ADD/REDUCE/FORCE_FLAT）时立即推
    """
    if not cfg.get("mock_short_enabled", False):
        return
    ms_cfg = cfg.get("mock_short", {})
    if not ms_cfg.get("active", False):
        return

    ms_state = state.setdefault("mock_short", {})

    # 先尝试读实盘仓位（币安1 BTCUSDT 空单）
    role    = ms_cfg.get("role",    "玄玄")
    account = ms_cfg.get("account", "币安1")
    symbol  = ms_cfg.get("symbol",  "BTCUSDT")
    live    = _read_live_short(role, account, symbol)
    is_live = live is not None
    log.info(f"[mock_short] branch={'LIVE' if is_live else 'MOCK'} account={account} symbol={symbol}")

    if is_live:
        avg_entry = live["entry"]
        leverage  = live["leverage"]
        liq_price = live["liq"]
    else:
        liq_price = float(ms_cfg.get("liq_price", 88000))
        leverage  = float(ms_cfg.get("leverage", 3))
        if not ms_state.get("entry_price"):
            ms_state["entry_price"] = price
            ms_state["entry_time"]  = datetime.now(BJ).isoformat()
        avg_entry = float(ms_state["entry_price"])

    # 真实动作检测：区段变化 且 action 含 ADD/REDUCE/FORCE_FLAT
    cur_action  = zone.get("action", "")
    last_action = ms_state.get("last_push_action", "")
    is_real_action = (
        cur_action != last_action and
        any(x in cur_action for x in ("ADD", "REDUCE", "FORCE_FLAT"))
    )

    # 实盘事件触发：exec_log 有新 account+symbol 真实动作就立即推
    last_exec_ts = ms_state.get("last_exec_ts", "")
    has_new_exec, new_latest_exec_ts = _check_new_exec_ts(account, symbol, last_exec_ts)

    # 推送门：定点 / tide区段动作 / 实盘新动作，三者居一
    now      = datetime.now(BJ)
    minute   = now.minute
    last_min = state.get("mock_last_push_minute", -1)
    if not (is_real_action or has_new_exec):
        if minute not in _PUSH_MINUTES or minute == last_min:
            return

    # 大箱：底/顶/中（中=几何中点，从 mother 算，忽略 cfg.center_axis 遗留字段）
    mother = cfg.get("box", {}).get("mother", {}) or {}
    big_low  = float(mother.get("lower", 58000))
    big_high = float(mother.get("upper", 88000))
    big_axis = (big_low + big_high) / 2

    # 小箱：底/顶/中（优先 state.small_box 动态值）
    sm = state.get("small_box") or {}
    sm_cfg = cfg.get("box", {}).get("small", {}) or {}
    sm_low  = float(sm.get("lower") or sm_cfg.get("lower", big_low))
    sm_high = float(sm.get("upper") or sm_cfg.get("upper", big_high))
    sm_mid  = float(sm.get("mid")   or sm_cfg.get("center", (sm_low + sm_high) / 2))

    # 方向挂大箱（做空视角：目标对岸=58000 大箱下沿）
    if price < big_axis:
        direction = "🟢接近对岸"
    elif price > big_axis:
        direction = "🔴背离对岸"
    else:
        direction = "✅本岸"

    # 盈亏（做空：跌赚涨亏）
    pnl_pct = (avg_entry - price) / avg_entry * 100 * leverage
    pnl_str = f"🍀 盈利 +{pnl_pct:.1f}%" if pnl_pct >= 0 else f"💔 亏损 {pnl_pct:.1f}%"

    # 距强平
    liq_dist = (liq_price - price) / price * 100

    # 最近动作流水
    # 实盘：从 exec_log 读最近 3 条真实动作
    # 模拟：用 zone.action 切换入队（ADD/REDUCE/FORCE_FLAT）
    if is_live:
        live_actions = _read_recent_actions(account, symbol, n=3)
        action_str = " → ".join(live_actions) if live_actions else "等待动作"
    else:
        history = ms_state.get("action_history")
        if not history:
            history = ["开空"]
            ms_state["action_history"] = history
        if is_real_action:
            history.append(_zh(cur_action))
            if len(history) > 3:
                del history[:-3]
        action_str = " → ".join(history)

    footer = (
        f"🔴 实盘 · 账户 {account}"
        if is_live
        else "⚠️ 模拟盘，非真实下单"
    )

    text = (
        f"🌊 <b>小刃 潮汐-BTC实时状态</b>\n"
        f"价格：<b>${price:,.0f}</b>\n"
        f"大箱：底 ${big_low:,.0f}  顶 ${big_high:,.0f}  中 ${big_axis:,.0f}  {direction}\n"
        f"小箱：底 ${sm_low:,.0f}  顶 ${sm_high:,.0f}  中 ${sm_mid:,.0f}\n"
        f"做空均价：${avg_entry:,.0f}  {pnl_str}\n"
        f"强平：${liq_price:,.0f}  距强平：+{liq_dist:.1f}%\n"
        f"最近动作：{action_str}\n"
        f"─────────────────\n"
        f"{footer}"
    )

    _send(_BAOBAO_TOKEN, _GROUP_CHAT, text)

    state["mock_last_push_minute"] = minute
    ms_state["last_push_action"]   = cur_action
    ms_state["last_exec_ts"]       = new_latest_exec_ts
    write_state_fn(state)
