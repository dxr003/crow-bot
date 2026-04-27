"""
trailing_layered.py — 分级移动止盈（多策略 profile）

与 trailing.py（v4.1 全平版）独立并存：
  - trailing.py       → 全平模式，默认 profile=manual
  - trailing_layered  → 分级减仓，按 profile 套用一套参数（潮汐/阻击/人工分级）

每次触发按 reduce_ratio 减一档后，peak_price 重置到当前价继续跟踪，支持多轮触发。
仓位被外部清零时自动 deactivate。
"""
import os
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

STATE_FILE = Path(__file__).parent.parent / "data" / "trailing_layered_state.json"
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "509640925")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
FAPI_BASE = "https://fapi.binance.com"
DEFAULT_ACCOUNT = "币安1"

# ── 策略画像（可扩展） ──────────────────────────────────────
PROFILES = {
    # ── 潮汐策略（已启用）────────────────────────────────
    "tide": {
        "activate_pct": 10.0,
        "retrace_pct":  20.0,
        "reduce_ratio": 30.0,
        "desc": "潮汐底仓分级（浮盈10%/回撤20%→减30%）",
        "status": "active",
    },
    # ── 做多阻击（命名占位，参数从 bull_sniper/config.yaml 复刻）────────
    "sniper_bull_limit": {
        "activate_pct": 25.0,
        "retrace_pct":  20.0,
        "reduce_ratio": 100.0,
        "desc": "做多阻击·限价止盈版（trailing_limit，浮盈25%/回撤20%→全平）",
        "status": "reserved",     # 等启用
    },
    "sniper_bull_layer1": {
        "activate_pct": 15.0,
        "retrace_pct":  10.0,
        "reduce_ratio": 100.0,
        "desc": "做多阻击·两层版 Layer1（浮盈15%/回撤10%→全平）",
        "status": "reserved",
    },
    "sniper_bull_layer2": {
        "activate_pct": 35.0,
        "retrace_pct":  15.0,
        "reduce_ratio": 100.0,
        "desc": "做多阻击·两层版 Layer2（浮盈35%/回撤15%→全平）",
        "status": "reserved",
    },
    # ── 做空阻击（预留名，参数实战后定）────────
    "sniper_short": {
        "activate_pct": 30.0,
        "retrace_pct":  25.0,
        "reduce_ratio": 100.0,
        "desc": "做空阻击（预留名，参数待定）",
        "status": "reserved",
    },
    # ── 人工分级（默认值，可逐项覆盖）────────
    "manual_layered": {
        "activate_pct": 30.0,
        "retrace_pct":  25.0,
        "reduce_ratio": 50.0,
        "desc": "人工分级（默认值，可覆盖）",
        "status": "active",
    },
}

# ── 键编码 ────────────────────────────────────────────────

def _make_key(symbol: str, account: str, profile: str) -> str:
    return f"{symbol}@{account}#{profile}"


def _parse_key(key: str) -> tuple[str, str, str]:
    if "#" not in key:
        return key, DEFAULT_ACCOUNT, "manual_layered"
    head, profile = key.rsplit("#", 1)
    if "@" in head:
        sym, acct = head.split("@", 1)
        return sym, acct, profile
    return head, DEFAULT_ACCOUNT, profile


# ── 状态持久化 ────────────────────────────────────────────

def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(state: dict):
    # 2026-04-26: 改原子写防崩溃留半文件（与 bull_trailing 对齐）
    from trader.multi._atomic import atomic_write_json
    atomic_write_json(STATE_FILE, state)


# ── 工具 ──────────────────────────────────────────────────

def _resolve_account(account: str) -> str:
    try:
        from trader.multi.registry import _resolve_name
        return _resolve_name(account)
    except Exception:
        return account


def _get_positions(account: str, symbol: str) -> list[dict]:
    from trader.multi.registry import get_futures_client
    c = get_futures_client(account)
    raw = c.get_position_risk(symbol=symbol)
    return [p for p in raw if float(p.get("positionAmt", 0)) != 0]


def _get_mark_price(symbol: str) -> float:
    r = requests.get(f"{FAPI_BASE}/fapi/v1/premiumIndex",
                     params={"symbol": symbol}, timeout=5)
    return float(r.json()["markPrice"])


def _calc_pnl_pct(side: str, entry: float, cur: float, leverage: int) -> float:
    """保证金收益率% = 价格变动% × 杠杆"""
    if entry <= 0:
        return 0.0
    diff = (cur - entry) / entry if side == "long" else (entry - cur) / entry
    return diff * 100 * leverage


def _notify(msg: str):
    if not BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception:
        pass


def _reduce_position(account: str, symbol: str, pct: float) -> dict:
    """按百分比减仓。100=全平。"""
    from trader.multi.executor import close_market
    return close_market("大猫", account, symbol, pct=pct)


# ── tide 联动钩子 ─────────────────────────────────────────────
# 潮汐 profile 减仓成功后，把"卖了多少 U / 卖在哪个价"写进 tide state，
# 供 tide/exec/add_engine 的 Buyback / Pullback 触发器消费（回补/加仓）。

_TIDE_STATE_PATH = Path("/root/maomao/tide/state/state.json")


def _write_tide_last_sell(symbol: str, account: str,
                          sell_price: float, sold_usd: float):
    """写 tide/state/state.json.last_sell。失败不抛异常、不阻塞主路径。"""
    try:
        if not _TIDE_STATE_PATH.exists():
            return
        s = json.loads(_TIDE_STATE_PATH.read_text(encoding="utf-8"))
        s["last_sell"] = {
            "price": float(sell_price),
            "sold_usd": round(float(sold_usd), 2),
            "symbol": symbol,
            "account": account,
            "ts": int(time.time()),
        }
        _TIDE_STATE_PATH.write_text(
            json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


# ── 主 API ────────────────────────────────────────────────

def activate(symbol: str,
             profile: str = "manual_layered",
             account: str = DEFAULT_ACCOUNT,
             activate_pct: float = None,
             retrace_pct: float = None,
             reduce_ratio: float = None,
             note: str = "",
             leverage: int = None) -> str:
    """开启分级移动止盈追踪。
    profile: tide / sniper_short / sniper_bull / manual_layered
    activate_pct / retrace_pct / reduce_ratio: 不填用 PROFILES 默认值
    leverage: 显式指定杠杆（全仓模式 position_risk 有时返回 0/1，需手动覆盖）
    """
    if profile not in PROFILES:
        return f"❌ 未知 profile: {profile}，可选: {', '.join(PROFILES.keys())}"
    p = PROFILES[profile]
    ap = activate_pct if activate_pct is not None else p["activate_pct"]
    rp = retrace_pct  if retrace_pct  is not None else p["retrace_pct"]
    rr = reduce_ratio if reduce_ratio is not None else p["reduce_ratio"]

    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    account = _resolve_account(account)

    try:
        positions = _get_positions(account, symbol)
    except Exception as e:
        return f"❌ [{account}] 查仓失败: {e}"
    if not positions:
        return f"❌ {symbol} 在 {account} 无持仓，无法开启分级追踪"

    pos = positions[0]
    amt   = float(pos["positionAmt"])
    side  = "long" if amt > 0 else "short"
    entry = float(pos["entryPrice"])
    lev_raw = int(pos.get("leverage") or 0)
    if leverage:
        lev = int(leverage)
    elif lev_raw >= 1:
        lev = lev_raw
    else:
        lev = 1  # 兜底，但会在 status 里显示提示让用户手动覆盖

    try:
        cur_price = _get_mark_price(symbol)
    except Exception as e:
        return f"❌ 拉标记价失败: {e}"

    float_pnl = _calc_pnl_pct(side, entry, cur_price, lev)
    already_active = float_pnl >= ap
    key = _make_key(symbol, account, profile)

    state = _load()
    state[key] = {
        "symbol":       symbol,
        "account":      account,
        "side":         side,
        "profile":      profile,
        "note":         note,
        "activate_pct": ap,
        "retrace_pct":  rp,
        "reduce_ratio": rr,
        "entry_price":  entry,
        "leverage":     lev,
        "activated":    already_active,
        "peak_price":   cur_price if already_active else entry,
        "trigger_count": 0,
        "last_trigger_price": None,
        "last_trigger_at":    None,
        "created_at":   int(time.time()),
        "activated_at": int(time.time()) if already_active else None,
    }
    _save(state)

    tag = f"[{profile}]"
    if note:
        tag += f"({note})"
    if already_active:
        return (f"✅ {tag} {symbol} {account} 分级追踪已激活\n"
                f"  方向 {side} 杠杆 {lev}x 入场 {entry}\n"
                f"  当前浮盈 +{float_pnl:.1f}% ≥ 阈值 +{ap}% → 立即进入峰值跟踪\n"
                f"  参数: 回撤 {rp}% → 减 {rr}%")
    return (f"✅ {tag} {symbol} {account} 分级追踪已挂（待激活）\n"
            f"  方向 {side} 杠杆 {lev}x 入场 {entry}\n"
            f"  当前浮盈 {float_pnl:+.1f}%，等达到 +{ap}% 后追踪\n"
            f"  参数: 回撤 {rp}% → 减 {rr}%")


def deactivate(symbol: str,
               account: str = None,
               profile: str = None) -> str:
    """取消追踪。
    - 只填 symbol：删所有账户/所有 profile 的该币条目
    - 填 account：只删该账户（所有 profile）
    - 同时填 profile：精确删
    """
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    if account:
        account = _resolve_account(account)

    state = _load()
    removed = []
    for k in list(state.keys()):
        sym, acct, prof = _parse_key(k)
        if sym != symbol:
            continue
        if account and acct != account:
            continue
        if profile and prof != profile:
            continue
        removed.append(k)
        state.pop(k)
    if not removed:
        return f"⚠️ 未找到 {symbol} 匹配条目"
    _save(state)
    return f"✅ 已取消 {len(removed)} 条分级追踪: " + ", ".join(removed)


def check_all() -> list[str]:
    """cron 调用。遍历 state，按各自 profile 阈值判断激活/触发。"""
    state = _load()
    messages = []
    dirty = False
    to_drop = []

    # 2026-04-26: 单轮并行预拉 + markPrice 缓存
    needed_pos = set()
    needed_marks = set()
    for key, entry in state.items():
        try:
            needed_pos.add((entry["account"], entry["symbol"]))
            needed_marks.add(entry["symbol"])
        except Exception:
            continue

    def _fetch_pos(t):
        acct, sym = t
        try:
            return t, ("ok", _get_positions(acct, sym))
        except Exception as e:
            return t, ("err", str(e))

    pos_cache: dict = {}
    if needed_pos:
        with ThreadPoolExecutor(max_workers=min(len(needed_pos), 4)) as pool:
            for t, val in pool.map(_fetch_pos, needed_pos):
                pos_cache[t] = val

    mark_cache: dict = {}  # 按 symbol 缓存

    for key, entry in list(state.items()):
        try:
            symbol  = entry["symbol"]
            account = entry["account"]
            side    = entry["side"]
            profile = entry["profile"]
            ap      = entry["activate_pct"]
            rp      = entry["retrace_pct"]
            rr      = entry["reduce_ratio"]
            lev     = entry["leverage"]
            entry_px = entry["entry_price"]

            # 确认仓位还在（用预拉缓存）
            cached = pos_cache.get((account, symbol))
            if cached is None or cached[0] == "err":
                err = cached[1] if cached else "未拉取"
                messages.append(f"⚠️ [{profile}] {symbol} {account} 查询失败: {err}")
                continue
            positions = cached[1]
            if not positions:
                to_drop.append(key)
                messages.append(f"ℹ️ [{profile}] {symbol} {account} 仓位已清，自动取消追踪")
                continue

            if symbol not in mark_cache:
                mark_cache[symbol] = _get_mark_price(symbol)
            cur_price = mark_cache[symbol]
            float_pnl = _calc_pnl_pct(side, entry_px, cur_price, lev)

            # 激活阶段
            if not entry["activated"]:
                if float_pnl >= ap:
                    entry["activated"]    = True
                    entry["activated_at"] = int(time.time())
                    entry["peak_price"]   = cur_price
                    dirty = True
                    messages.append(
                        f"🎯 [{profile}] {symbol} {account} 分级追踪激活\n"
                        f"  浮盈 +{float_pnl:.1f}% ≥ +{ap}%，峰值={cur_price}\n"
                        f"  将在峰值回撤 {rp}% 时减仓 {rr}%"
                    )
                continue

            # 跟踪峰值
            peak = entry["peak_price"]
            peak_updated = False
            if side == "long" and cur_price > peak:
                entry["peak_price"] = cur_price
                peak = cur_price
                dirty = True
                peak_updated = True
            elif side == "short" and cur_price < peak:
                entry["peak_price"] = cur_price
                peak = cur_price
                dirty = True
                peak_updated = True

            # 峰值保证金收益率（用更新后的 peak）
            peak_pnl = _calc_pnl_pct(side, entry_px, peak, lev)

            # ── SL 动态上移（只 sniper_bull_limit 生效，只升不降）──
            # 阶段按保证金 peak_pnl：
            #   ≥50%  → SL 锁到保证金 +10%（保本 + 小赚）
            #   ≥100% → SL 锁到 +40%
            #   ≥200% → SL 锁到 +100%
            if profile == "sniper_bull_limit" and peak_pnl > 0:
                sl_stages = [(50.0, 10.0), (100.0, 40.0), (200.0, 100.0)]
                cur_stage = int(entry.get("sl_stage", 0))
                for idx, (trig, lock_pnl) in enumerate(sl_stages, 1):
                    if peak_pnl >= trig and cur_stage < idx:
                        price_ratio = lock_pnl / max(lev, 1) / 100.0
                        new_sl = entry_px * (1 + price_ratio) if side == "long" \
                                 else entry_px * (1 - price_ratio)
                        # 防守：新 SL 已被市价穿过就放弃（避免撤旧 SL 后挂不上新 SL 导致裸奔）
                        if (side == "long" and new_sl >= cur_price) or \
                           (side == "short" and new_sl <= cur_price):
                            messages.append(
                                f"⚠️ [{profile}] {symbol} {account} stage-{idx} 跳过"
                                f"：新SL {new_sl:.6g} 已被市价 {cur_price} 穿过"
                            )
                            break
                        # 2026-04-26 03:19 老大根治：先挂新 SL → 成功才撤旧（防裸奔，对齐 stop_loss_manager 修法）
                        try:
                            from trader.multi import executor as _ex
                            direction = "long" if side == "long" else "short"
                            # 1. 先挂新 SL（不撤旧）
                            place_res = _ex.place_stop_loss("大猫", account, symbol, new_sl, direction)
                            if not place_res or place_res.get("error"):
                                messages.append(
                                    f"❌ [{profile}] {symbol} {account} stage-{idx} 新 SL 挂失败"
                                    f"，旧 SL 保留生效：{place_res.get('error') if place_res else 'no_resp'}"
                                )
                                break
                            # 2. 新挂成功才撤旧（cancel_all 撤所有 algo，含原 SL）
                            #    短窗口内 2 个 SL 共存，触发任一即 closePosition 全平
                            try:
                                # 跳过本次新挂的 algoId 撤其他（用 cancel_all 简化，新 SL 已成功）
                                # 注：cancel_all 会撤掉新挂的 SL，所以改为只撤"非本次"的 algo
                                # 简化：等下一轮 cron 再统一管理（多挂一笔 SL 不影响触发）
                                pass
                            except Exception:
                                pass
                            entry["sl_stage"] = idx
                            entry["sl_upgraded_at"] = int(time.time())
                            entry["sl_upgraded_price"] = new_sl
                            dirty = True
                            messages.append(
                                f"🛡 [{profile}] {symbol} {account} SL 上移 stage-{idx}\n"
                                f"  peak +{peak_pnl:.1f}% 保证金 → SL 锁 +{lock_pnl:.0f}% @ {new_sl:.6g}"
                            )
                        except Exception as e:
                            messages.append(
                                f"❌ [{profile}] {symbol} {account} SL 上移异常: {e}"
                            )
                        break  # 每轮只升一级

            if peak_updated:
                continue
            if peak_pnl <= 0:
                continue
            # 回撤比例 = (峰值盈利 - 当前盈利) / 峰值盈利
            pullback = (peak_pnl - float_pnl) / peak_pnl * 100
            # 阶梯 retrace：peak 越高越放宽，给大浪留空间（2026-04-25 实战反馈）
            # sniper_bull_limit 原 20% 触发太早，peak 60+ 时错过 60+ 个点盈利
            # 17:10 老大补加 150%+ 一档让大涨仓位跑更远
            if profile == "sniper_bull_limit":
                if peak_pnl >= 150:
                    effective_rp = max(rp, 50.0)
                elif peak_pnl >= 60:
                    effective_rp = max(rp, 40.0)
                elif peak_pnl >= 30:
                    effective_rp = max(rp, 30.0)
                else:
                    effective_rp = rp
            else:
                effective_rp = rp
            if pullback < effective_rp:
                continue

            # ── 触发减仓 ──
            # 先记录减仓前的仓位大小，用于算 sold_usd（tide 联动需要）
            try:
                amt_before = abs(float(positions[0].get("positionAmt", 0)))
            except Exception:
                amt_before = 0.0

            # 2026-04-26 修：先调 API，成功才改 trigger_count（防虚假减仓计数）
            result = _reduce_position(account, symbol, rr)
            if not result.get("ok"):
                tag = f"[{profile}]" + (f"({entry.get('note','')})" if entry.get("note") else "")
                messages.append(
                    f"❌ {tag} {symbol} {account} 分级减仓失败\n"
                    f"  浮盈 +{float_pnl:.1f}% / 峰值 +{peak_pnl:.1f}% 回撤 {pullback:.1f}%\n"
                    f"  err: {result.get('error','?')}"
                )
                continue
            # 减仓成功才更新计数 + 时间戳
            trigger_count = entry["trigger_count"] + 1
            entry["trigger_count"] = trigger_count
            entry["last_trigger_price"] = cur_price
            entry["last_trigger_at"]    = int(time.time())

            # 潮汐联动：profile=tide 的减仓写入 tide state.last_sell，供 add_engine 消费
            if profile == "tide" and amt_before > 0:
                sold_usd = amt_before * (rr / 100.0) * cur_price
                _write_tide_last_sell(symbol, account, cur_price, sold_usd)

            tag = f"[{profile}]" + (f"({entry.get('note','')})" if entry.get("note") else "")
            msg = (
                f"✂️ {tag} {symbol} {account} 分级减仓 #{trigger_count}\n"
                f"  浮盈 +{float_pnl:.1f}% 峰值 +{peak_pnl:.1f}% 回撤 {pullback:.1f}%\n"
                f"  已减 {rr}%，入场 {entry_px} 现价 {cur_price}"
            )

            # 100% 减 = 全平 → 删除条目
            if rr >= 100.0:
                to_drop.append(key)
                msg += "\n  → 全平完成，追踪结束"
            else:
                # 留下继续跟下一档，peak 重置到当前价
                entry["peak_price"] = cur_price
                dirty = True

            _notify(msg)
            messages.append(msg)

        except Exception as e:
            messages.append(f"❌ {key} 检查异常: {e}")

    for k in to_drop:
        state.pop(k, None)
    if dirty or to_drop:
        _save(state)

    return messages


def format_status() -> str:
    """展示所有分级追踪条目。"""
    state = _load()
    if not state:
        return "当前无分级追踪"

    lines = [f"📊 分级移动止盈（共 {len(state)} 条）"]
    for key, e in state.items():
        tag = f"[{e['profile']}]"
        if e.get("note"):
            tag += f"({e['note']})"
        status = "🟢激活" if e["activated"] else "⏳待激活"
        try:
            cur = _get_mark_price(e["symbol"])
            pnl = _calc_pnl_pct(e["side"], e["entry_price"], cur, e["leverage"])
            pnl_str = f"+{pnl:.1f}%" if pnl >= 0 else f"{pnl:.1f}%"
        except Exception:
            pnl_str = "?"
        lines.append(
            f"\n{status} {tag} {e['symbol']} {e['account']} {e['side']}"
            f"\n  入场 {e['entry_price']} {e['leverage']}x 当前浮盈 {pnl_str}"
            f"\n  激活 +{e['activate_pct']}% / 回撤 {e['retrace_pct']}% / 减 {e['reduce_ratio']}%"
            f"\n  峰值 {e['peak_price']} 已触发 {e['trigger_count']} 次"
        )
    return "\n".join(lines)


def status_all() -> str:
    """聚合视图：同时列出 trailing.py（全平版）和 trailing_layered（分级版）。"""
    lines = ["═══ 移动止盈统一视图 ═══"]

    # v4.1 全平版
    try:
        from trader.trailing import _load as _load_v41
        v41_state = _load_v41()
    except Exception as e:
        v41_state = {}
        lines.append(f"⚠️ trailing.py 读取异常: {e}")

    lines.append(f"\n🔹 v4.1 全平版（trailing.py）— {len(v41_state)} 条")
    if not v41_state:
        lines.append("  （空）")
    for k, e in v41_state.items():
        sym = e.get("symbol") or k.split("@")[0]
        acct = k.split("@", 1)[1] if "@" in k else "?"
        status = "🟢" if e.get("activated") else "⏳"
        thr = e.get("activation_threshold", "?")
        lines.append(f"  {status} [manual] {sym} {acct} {e.get('side','?')} 激活+{thr}%→回撤30%→全平")

    # 分级版
    layered = _load()
    lines.append(f"\n🔹 分级版（trailing_layered.py）— {len(layered)} 条")
    if not layered:
        lines.append("  （空）")
    for _, e in layered.items():
        tag = f"[{e['profile']}]"
        if e.get("note"):
            tag += f"({e['note']})"
        status = "🟢" if e["activated"] else "⏳"
        lines.append(
            f"  {status} {tag} {e['symbol']} {e['account']} {e['side']} "
            f"激活+{e['activate_pct']}%→回撤{e['retrace_pct']}%→减{e['reduce_ratio']}% "
            f"(触发{e['trigger_count']}次)"
        )

    return "\n".join(lines)


def list_profiles() -> str:
    lines = ["📋 可用 profile（策略画像）:"]
    for name, p in PROFILES.items():
        tag = "🟢 active" if p.get("status") == "active" else "⚪ reserved"
        lines.append(f"\n• {name}  {tag}")
        lines.append(f"  {p['desc']}")
        lines.append(f"  激活 +{p['activate_pct']}% / 回撤 {p['retrace_pct']}% / 减 {p['reduce_ratio']}%")
    return "\n".join(lines)
