"""
trailing.py — 移动止盈 v4.1（多账户）
规则：保证金浮盈达到激活阈值后追踪峰值，回撤利润30%（+3%容错防抖）触发全平
浮盈按保证金收益率计算：价格变动% × 杠杆

多账户（2026-04-19）：
  - 状态键：`symbol@account`（默认 account="币安1"，老 `symbol` 键自动迁移到 `symbol@币安1`）
  - activate/deactivate 接 account 参数，默认 币安1，玄玄一键调用不受影响
  - check_all 按键前缀路由到对应账户客户端

API：
  activate(symbol, threshold=50, account="币安1")  → 开启追踪
  deactivate(symbol, account=None)                  → 取消追踪（None=该币所有账户都取消）
  check_all()                                       → cron调用，检查并触发平仓
  format_status()                                   → 玄玄展示当前追踪列表
"""
import os
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

STATE_FILE = Path(__file__).parent.parent / "data" / "trailing_state.json"

PULLBACK_TRIGGER = float(os.getenv("TRAILING_PULLBACK",   30))   # 回撤利润30%触发
TOLERANCE        = float(os.getenv("TRAILING_TOLERANCE",   3))   # 容错防抖%
DEFAULT_ACTIVATE = float(os.getenv("TRAILING_ACTIVATION", 50))   # 默认激活阈值%（保证金收益率）

CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "509640925")

DEFAULT_ACCOUNT = "币安1"
FAPI_BASE = "https://fapi.binance.com"


# ── 键编码/解码 ───────────────────────────────────────────

def _make_key(symbol: str, account: str) -> str:
    return f"{symbol}@{account}"


def _parse_key(key: str) -> tuple[str, str]:
    """返回 (symbol, account)。老键（纯symbol）回退到 DEFAULT_ACCOUNT。"""
    if "@" in key:
        sym, acct = key.split("@", 1)
        return sym, acct
    return key, DEFAULT_ACCOUNT


def _migrate_legacy(state: dict) -> tuple[dict, bool]:
    """把老 symbol 键迁移到 symbol@币安1；返回 (新state, 是否有改动)。"""
    changed = False
    new_state = {}
    for k, v in state.items():
        if "@" in k:
            new_state[k] = v
        else:
            new_state[_make_key(k, DEFAULT_ACCOUNT)] = v
            changed = True
    return new_state, changed


# ── 状态持久化 ────────────────────────────────────────────

def _load() -> dict:
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    state, migrated = _migrate_legacy(raw)
    if migrated:
        _save(state)
    return state


def _save(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def update_peak(symbol: str, account: str, cur_price: float) -> bool:
    """公共接口：供 rolling 等外部模块在加仓后推高/压低峰值。
    long 取 max，short 取 min；未激活的条目不动。返回是否更新。"""
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    state = _load()
    key = _make_key(symbol, account)
    entry = state.get(key)
    if not entry or not entry.get("activated"):
        return False
    old_peak = entry["peak_price"]
    if entry["side"] == "long":
        new_peak = max(old_peak, cur_price)
    else:
        new_peak = min(old_peak, cur_price)
    if new_peak == old_peak:
        return False
    entry["peak_price"] = new_peak
    state[key] = entry
    _save(state)
    return True


# ── 账户桥接（通过 multi.registry） ─────────────────────────

def _futures_client(account: str):
    """获取指定账户的 UMFutures 客户端"""
    from trader.multi.registry import get_futures_client
    return get_futures_client(account)


def _get_positions_acct(account: str, symbol: str) -> list[dict]:
    """拉某账户指定币种的非零持仓"""
    try:
        c = _futures_client(account)
        raw = c.get_position_risk(symbol=symbol)
        return [p for p in raw if float(p.get("positionAmt", 0)) != 0]
    except Exception as e:
        raise RuntimeError(f"{account} 拉持仓失败: {e}")


def _get_mark_price(symbol: str) -> float:
    """标记价公开端点，不区分账户"""
    resp = requests.get(
        f"{FAPI_BASE}/fapi/v1/premiumIndex",
        params={"symbol": symbol}, timeout=5,
    )
    return float(resp.json()["markPrice"])


def _cancel_all_orders_acct(account: str, symbol: str) -> None:
    """撤某账户该币种所有挂单（含条件单）"""
    try:
        c = _futures_client(account)
        try:
            c.cancel_open_orders(symbol=symbol)
        except Exception:
            pass
    except Exception:
        pass


def _close_market_acct(account: str, symbol: str) -> str:
    """市价平仓。币安1 走 trader.order 保留 dark_order；其他走 multi.executor.close_market"""
    if account == DEFAULT_ACCOUNT:
        # 保留原有 dark_order 行为
        from trader.order import execute
        from trader.exchange import get_positions
        positions = get_positions(symbol)
        if not positions:
            return f"⚠️ {symbol} 无持仓"
        amt = float(positions[0]["positionAmt"])
        action = "close_long" if amt > 0 else "close_short"
        return execute({"action": action, "symbol": symbol, "dark_order": True})
    # 其他账户：走 multi.executor
    from trader.multi.executor import close_market
    try:
        r = close_market("大猫", account, symbol, pct=100.0)
        if r.get("error"):
            return f"❌ {account} 平仓失败: {r['error']}"
        return f"✅ {account} 已平仓 orderId={r.get('orderId','?')}"
    except Exception as e:
        return f"❌ {account} 平仓异常: {e}"


# ── 核心操作 ──────────────────────────────────────────────

def activate(symbol: str, threshold: float = None, account: str = DEFAULT_ACCOUNT) -> str:
    """
    为当前持仓开启移动止盈追踪。
    threshold: 激活浮盈阈值%，None 用默认值
    account: 账户别名，默认 币安1
    """
    if threshold is None:
        threshold = DEFAULT_ACTIVATE

    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    # 解析别名
    try:
        from trader.multi.registry import _resolve_name
        account = _resolve_name(account)
    except Exception:
        pass

    try:
        positions = _get_positions_acct(account, symbol)
    except Exception as e:
        return f"❌ {account} 查询失败: {e}"
    if not positions:
        return f"❌ {symbol} 在 {account} 无持仓，无法开启移动止盈"

    pos = positions[0]
    amt         = float(pos["positionAmt"])
    side        = "long" if amt > 0 else "short"
    entry_price = float(pos["entryPrice"])
    notional    = abs(float(pos.get("notional", 0)))
    init_margin = float(pos.get("initialMargin", 0))
    leverage    = max(1, round(notional / init_margin)) if init_margin > 0 else 1
    cur_price   = _get_mark_price(symbol)

    float_pnl = _calc_pnl(side, entry_price, cur_price, leverage)
    already_active = float_pnl >= threshold

    state = _load()
    key = _make_key(symbol, account)
    state[key] = {
        "account":             account,
        "side":                side,
        "entry_price":         entry_price,
        "leverage":            leverage,
        "activation_threshold": threshold,
        "activated":           already_active,
        "peak_price":          cur_price if already_active else entry_price,
        "started_at":          int(time.time()),
        "activated_at":        int(time.time()) if already_active else None,
    }
    _save(state)

    coin = symbol.replace("USDT", "")
    tag = "" if account == DEFAULT_ACCOUNT else f"（{account}）"
    if already_active:
        return (
            f"✅ {coin}{tag} 移动止盈已激活\n"
            f"方向: {'多' if side == 'long' else '空'}  {leverage}x  入场: {entry_price}\n"
            f"保证金浮盈: +{float_pnl:.1f}%  峰值: {cur_price}\n"
            f"回撤{PULLBACK_TRIGGER}%利润触发全平"
        )
    return (
        f"✅ {coin}{tag} 移动止盈已设置\n"
        f"方向: {'多' if side == 'long' else '空'}  {leverage}x  入场: {entry_price}\n"
        f"保证金浮盈: {float_pnl:.1f}%，等待达到 +{threshold}% 后开始追踪"
    )


def deactivate(symbol: str, account: str = None) -> str:
    """
    取消某持仓的移动止盈追踪。
    account=None：取消该币所有账户的追踪
    account=指定：只取消该账户的追踪
    """
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    state = _load()
    if account is None:
        # 取消该币所有账户
        removed = [k for k in state if _parse_key(k)[0] == symbol]
        if not removed:
            return f"❌ {symbol.replace('USDT','')} 未在追踪中"
        for k in removed:
            del state[k]
        _save(state)
        accts = [_parse_key(k)[1] for k in removed]
        return f"✅ {symbol.replace('USDT','')} 移动止盈已取消（{','.join(accts)}）"

    try:
        from trader.multi.registry import _resolve_name
        account = _resolve_name(account)
    except Exception:
        pass
    key = _make_key(symbol, account)
    if key not in state:
        return f"❌ {symbol.replace('USDT','')}@{account} 未在追踪中"
    del state[key]
    _save(state)
    return f"✅ {symbol.replace('USDT','')}@{account} 移动止盈已取消"


def format_status() -> str:
    """格式化当前追踪列表，供玄玄展示"""
    state = _load()
    if not state:
        return "当前无移动止盈追踪"

    lines = ["📊 移动止盈追踪中：\n"]
    for key, entry in state.items():
        symbol, account = _parse_key(key)
        coin        = symbol.replace("USDT", "")
        side        = entry["side"]
        entry_price = entry["entry_price"]
        threshold   = entry["activation_threshold"]
        activated   = entry["activated"]
        peak        = entry["peak_price"]
        leverage    = entry.get("leverage", 1)
        acct_tag    = "" if account == DEFAULT_ACCOUNT else f"@{account}"

        try:
            cur_price = _get_mark_price(symbol)
            float_pnl = _calc_pnl(side, entry_price, cur_price, leverage)
            pnl_str   = f"{float_pnl:+.1f}%"
        except Exception:
            pnl_str = "获取中"

        direction = "多" if side == "long" else "空"

        if activated:
            peak_pnl_val = _calc_pnl(side, entry_price, peak, leverage)
            lines.append(
                f"{coin}{acct_tag} {direction} {leverage}x  追踪中🟢\n"
                f"  保证金浮盈: {pnl_str}  峰值收益: +{peak_pnl_val:.1f}%\n"
                f"  回撤{PULLBACK_TRIGGER}%利润触发全平\n"
            )
        else:
            lines.append(
                f"{coin}{acct_tag} {direction} {leverage}x  等待激活⏳\n"
                f"  保证金浮盈: {pnl_str}  激活阈值: +{threshold}%\n"
            )

    return "\n".join(lines)


# ── cron 入口 ─────────────────────────────────────────────

def check_all() -> list:
    """
    检查所有追踪持仓，条件满足则触发平仓。
    返回触发列表，供 cron 脚本记录日志。
    """
    state = _load()
    if not state:
        return []

    triggered = []
    changed   = False

    # 2026-04-26: 单轮预拉所有 (account, symbol) 持仓 + markPrice 缓存
    # 避免循环里串行调 binance API，单账户卡死也不会堵全调度
    needed = {(_parse_key(k)[1], _parse_key(k)[0]) for k in state.keys()}  # (acct, sym)

    def _fetch_pos(t):
        acct, sym = t
        try:
            return t, ("ok", _get_positions_acct(acct, sym))
        except Exception as e:
            return t, ("err", str(e))

    pos_cache = {}
    if needed:
        with ThreadPoolExecutor(max_workers=min(len(needed), 4)) as pool:
            for t, val in pool.map(_fetch_pos, needed):
                pos_cache[t] = val

    mark_cache: dict = {}  # symbol -> price，单轮共享

    for key, entry in list(state.items()):
        symbol, account = _parse_key(key)
        try:
            cached = pos_cache.get((account, symbol))
            if cached is None or cached[0] == "err":
                err = cached[1] if cached else "未拉取"
                _notify(f"⚠️ {symbol.replace('USDT','')}@{account} 查询失败: {err}")
                continue
            positions = cached[1]

            if not positions:
                del state[key]
                changed = True
                _notify(f"ℹ️ {symbol.replace('USDT','')}@{account} 持仓已消失，移动止盈追踪自动清除")
                continue

            mp_raw = float(positions[0].get("markPrice") or 0)
            if mp_raw:
                cur_price = mp_raw
            else:
                if symbol not in mark_cache:
                    mark_cache[symbol] = _get_mark_price(symbol)
                cur_price = mark_cache[symbol]
            side        = entry["side"]
            entry_price = entry["entry_price"]
            leverage    = entry.get("leverage", 1)
            threshold   = entry["activation_threshold"]
            peak        = entry["peak_price"]
            float_pnl   = _calc_pnl(side, entry_price, cur_price, leverage)

            # ── 未激活：等待浮盈达阈值 ──
            if not entry["activated"]:
                if float_pnl >= threshold:
                    entry["activated"]    = True
                    entry["activated_at"] = int(time.time())
                    entry["peak_price"]   = cur_price
                    state[key] = entry
                    changed = True
                    coin = symbol.replace("USDT", "")
                    tp   = _trigger_price(side, cur_price)
                    acct_tag = "" if account == DEFAULT_ACCOUNT else f"（{account}）"
                    _notify(
                        f"🟢 {coin}{acct_tag} 移动止盈激活！\n"
                        f"浮盈 +{float_pnl:.1f}% 已达 +{threshold}%\n"
                        f"峰值: {cur_price}  触发价: {tp:.4f}"
                    )
                continue

            # ── 已激活：更新峰值 ──
            if side == "long" and cur_price > peak:
                entry["peak_price"] = cur_price
                state[key] = entry
                changed = True
                peak = cur_price
            elif side == "short" and cur_price < peak:
                entry["peak_price"] = cur_price
                state[key] = entry
                changed = True
                peak = cur_price

            # ── 计算利润回撤，判断是否触发 ──
            peak_pnl = _calc_pnl(side, entry_price, peak, leverage)
            if peak_pnl > 0:
                profit_drawdown = (peak_pnl - float_pnl) / peak_pnl * 100
            else:
                profit_drawdown = 0

            if profit_drawdown >= PULLBACK_TRIGGER + TOLERANCE:
                _cancel_all_orders_acct(account, symbol)
                result = _close_market_acct(account, symbol)

                del state[key]
                changed = True

                coin = symbol.replace("USDT", "")
                acct_tag = "" if account == DEFAULT_ACCOUNT else f"（{account}）"
                _notify(
                    f"🔔 移动止盈触发 — {coin}{acct_tag}\n"
                    f"方向: {'多' if side == 'long' else '空'}  杠杆: {leverage}x\n"
                    f"入场: {entry_price}  峰值: {peak:.4f}  当前: {cur_price:.4f}\n"
                    f"峰值保证金收益: +{peak_pnl:.1f}%  当前: {float_pnl:+.1f}%\n"
                    f"利润回撤: -{profit_drawdown:.1f}%\n"
                    f"{result}"
                )
                triggered.append({
                    "symbol":   symbol,
                    "account":  account,
                    "side":     side,
                    "pnl_pct":  round(float_pnl, 1),
                    "drawdown": round(profit_drawdown, 1),
                })

        except Exception as e:
            _notify(f"⚠️ {symbol.replace('USDT','')}@{account} 移动止盈检查出错: {e}")

    if changed:
        _save(state)

    return triggered


# ── 工具函数 ──────────────────────────────────────────────

def _calc_pnl(side: str, entry: float, cur: float, leverage: int = 1) -> float:
    """保证金收益率 = 价格变动% × 杠杆"""
    if side == "long":
        return (cur - entry) / entry * 100 * leverage
    return (entry - cur) / entry * 100 * leverage


def _drawdown(side: str, peak: float, cur: float) -> float:
    if side == "long":
        return (peak - cur) / peak * 100
    return (cur - peak) / peak * 100


def _trigger_price(side: str, peak: float) -> float:
    if side == "long":
        return peak * (1 - PULLBACK_TRIGGER / 100)
    return peak * (1 + PULLBACK_TRIGGER / 100)


def _notify(text: str):
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception:
        pass
