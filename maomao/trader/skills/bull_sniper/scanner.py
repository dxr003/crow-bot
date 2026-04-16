#!/usr/bin/env python3
"""
bull_sniper scanner.py — 做多阻击扫描器 v3.1

两层进池 + 观察池持续评分：
  启动黑名单：首次扫描，任意时段>15%排除
  第一层（瞬时触发，任意一条）：1m>12% / 3m>8% / 5m>7% / 15m>12%
  第二层（同时满足）：OI≥500万U
  观察池：零成本持续评分 → ≥38分推信号+AI决策
"""
import datetime as _dt
import json
import logging
import time
import yaml
import requests
from datetime import datetime
from pathlib import Path

from analyzer import analyze
from notifier import send_signal, send_health_report, send_status_card, send_trade_report, send_pool_entry
from buyer import execute as buyer_execute
from reject_tracker import record_exit as _record_reject, update_peaks as _update_reject_peaks

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR  = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "scanner.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("bull_scanner")


# ══════════════════════════════════════════
# 配置
# ══════════════════════════════════════════

def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", "r") as f:
        return yaml.safe_load(f)["bull_sniper"]

CFG = load_config()
_cfg_mtime = (BASE_DIR / "config.yaml").stat().st_mtime

def _hot_reload_config():
    """检查config.yaml是否被修改，有变化就热重载"""
    global CFG, _cfg_mtime
    try:
        mt = (BASE_DIR / "config.yaml").stat().st_mtime
        if mt != _cfg_mtime:
            CFG = load_config()
            _cfg_mtime = mt
            logger.info(f"[热重载] config.yaml 已更新，参数已生效")
    except Exception:
        pass

FAPI_BASE = "https://fapi.binance.com"

# 合约交易状态缓存（排除 SETTLING / 下架币）
_trading_symbols: set = set()
_trading_symbols_ts: float = 0


# ══════════════════════════════════════════
# 币安API
# ══════════════════════════════════════════

def _refresh_trading_symbols() -> set:
    """从 exchangeInfo 获取当前 TRADING 状态的合约，10分钟缓存"""
    global _trading_symbols, _trading_symbols_ts
    now = time.time()
    if _trading_symbols and now - _trading_symbols_ts < 600:
        return _trading_symbols
    try:
        resp = requests.get(f"{FAPI_BASE}/fapi/v1/exchangeInfo", timeout=15)
        resp.raise_for_status()
        symbols = set()
        for s in resp.json().get("symbols", []):
            if s.get("status") == "TRADING" and s["symbol"].endswith("USDT"):
                symbols.add(s["symbol"])
        _trading_symbols = symbols
        _trading_symbols_ts = now
        logger.info(f"[交易状态] 刷新完毕，{len(symbols)}个TRADING合约")
    except Exception as e:
        logger.warning(f"[交易状态] exchangeInfo获取失败: {e}")
    return _trading_symbols


def get_all_tickers() -> list:
    """获取全市场合约24h行情（自动排除非TRADING状态币）"""
    trading = _refresh_trading_symbols()
    resp = requests.get(f"{FAPI_BASE}/fapi/v1/ticker/24hr", timeout=10)
    resp.raise_for_status()
    tickers = []
    for t in resp.json():
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        # 排除 SETTLING / 下架 / 非TRADING状态
        if trading and sym not in trading:
            continue
        try:
            tickers.append({
                "symbol":      sym,
                "price":       float(t["lastPrice"]),
                "change_pct":  float(t["priceChangePercent"]),
                "volume_usdt": float(t["quoteVolume"]),
            })
        except (ValueError, KeyError):
            continue
    return tickers


def get_klines_1m(symbol: str, limit: int = 60) -> list:
    """获取1分钟K线"""
    resp = requests.get(
        f"{FAPI_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": "1m", "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_klines_1d(symbol: str) -> list:
    """获取日线K线，最大1500根覆盖全部上线历史"""
    resp = requests.get(
        f"{FAPI_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": "1d", "limit": 1500},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def calc_1h_change(symbol: str) -> float:
    """计算1小时涨幅：拉61根1分钟K线，用60分钟前开盘价vs当前收盘价"""
    try:
        klines = get_klines_1m(symbol, 61)
        if len(klines) < 61:
            return 0.0
        open_1h = float(klines[0][1])   # 60分钟前K线的开盘价
        close_now = float(klines[-1][4]) # 最新K线的收盘价
        return round((close_now - open_1h) / open_1h * 100, 2) if open_1h > 0 else 0.0
    except Exception as e:
        logger.debug(f"1h涨幅计算失败 {symbol}: {e}")
        return 0.0


def calc_1m_change(symbol: str) -> float:
    """计算1分钟涨幅：拉2根1分钟K线"""
    try:
        klines = get_klines_1m(symbol, 2)
        if len(klines) < 2:
            return 0.0
        open_1m = float(klines[0][1])
        close_now = float(klines[-1][4])
        return round((close_now - open_1m) / open_1m * 100, 2) if open_1m > 0 else 0.0
    except Exception as e:
        logger.debug(f"1m涨幅计算失败 {symbol}: {e}")
        return 0.0


def calc_3m_change(symbol: str) -> float:
    """计算3分钟涨幅：拉4根1分钟K线"""
    try:
        klines = get_klines_1m(symbol, 4)
        if len(klines) < 4:
            return 0.0
        open_3m = float(klines[0][1])
        close_now = float(klines[-1][4])
        return round((close_now - open_3m) / open_3m * 100, 2) if open_3m > 0 else 0.0
    except Exception as e:
        logger.debug(f"3m涨幅计算失败 {symbol}: {e}")
        return 0.0


def calc_2h_change(symbol: str) -> float:
    """计算2小时涨幅：拉121根1分钟K线，用120分钟前开盘价vs当前收盘价"""
    try:
        klines = get_klines_1m(symbol, 121)
        if len(klines) < 121:
            return 0.0
        open_2h = float(klines[0][1])
        close_now = float(klines[-1][4])
        return round((close_now - open_2h) / open_2h * 100, 2) if open_2h > 0 else 0.0
    except Exception as e:
        logger.debug(f"2h涨幅计算失败 {symbol}: {e}")
        return 0.0


def calc_5m_change(symbol: str) -> float:
    """计算5分钟涨幅：拉6根1分钟K线"""
    try:
        klines = get_klines_1m(symbol, 6)
        if len(klines) < 6:
            return 0.0
        open_5m = float(klines[0][1])
        close_now = float(klines[-1][4])
        return round((close_now - open_5m) / open_5m * 100, 2) if open_5m > 0 else 0.0
    except Exception as e:
        logger.debug(f"5m涨幅计算失败 {symbol}: {e}")
        return 0.0


def calc_15m_change(symbol: str) -> float:
    """计算15分钟涨幅：拉16根1分钟K线"""
    try:
        klines = get_klines_1m(symbol, 16)
        if len(klines) < 16:
            return 0.0
        open_15m = float(klines[0][1])
        close_now = float(klines[-1][4])
        return round((close_now - open_15m) / open_15m * 100, 2) if open_15m > 0 else 0.0
    except Exception as e:
        logger.debug(f"15m涨幅计算失败 {symbol}: {e}")
        return 0.0


def get_oi_usdt(symbol: str) -> float:
    """获取当前OI金额（张数×当前价=U）"""
    try:
        resp = requests.get(
            f"{FAPI_BASE}/fapi/v1/openInterest",
            params={"symbol": symbol},
            timeout=8,
        )
        resp.raise_for_status()
        oi_qty = float(resp.json()["openInterest"])
        # 拿当前价格换算成U
        price_resp = requests.get(
            f"{FAPI_BASE}/fapi/v1/ticker/price",
            params={"symbol": symbol},
            timeout=5,
        )
        price_resp.raise_for_status()
        price = float(price_resp.json()["price"])
        return oi_qty * price
    except Exception as e:
        logger.debug(f"OI获取失败 {symbol}: {e}")
        return 0.0


_oi_cache: dict = {}  # {symbol: {"value": float, "time": float}}

def get_oi_change(symbol: str) -> float:
    """获取OI变化百分比（当前OI vs 缓存的前值，缓存5分钟刷新）"""
    global _oi_cache
    now = time.time()
    try:
        resp = requests.get(
            f"{FAPI_BASE}/fapi/v1/openInterest",
            params={"symbol": symbol},
            timeout=8,
        )
        resp.raise_for_status()
        cur_oi = float(resp.json()["openInterest"])
    except Exception as e:
        logger.debug(f"OI获取失败 {symbol}: {e}")
        return 0.0

    prev = _oi_cache.get(symbol)
    if prev is None or now - prev["time"] >= 300:
        # 首次或缓存过期，存当前值作为基准，返回0
        _oi_cache[symbol] = {"value": cur_oi, "time": now}
        if prev is None:
            return 0.0
        # 有旧值，计算变化后更新缓存
        old_oi = prev["value"]
        _oi_cache[symbol] = {"value": cur_oi, "time": now}
        if old_oi <= 0:
            return 0.0
        return round((cur_oi - old_oi) / old_oi * 100, 2)

    # 缓存未过期，用缓存值计算变化
    old_oi = prev["value"]
    if old_oi <= 0:
        return 0.0
    return round((cur_oi - old_oi) / old_oi * 100, 2)


def get_lsr(symbol: str) -> float:
    """获取多空比"""
    try:
        resp = requests.get(
            f"{FAPI_BASE}/futures/data/topLongShortAccountRatio",
            params={"symbol": symbol, "period": "5m", "limit": 1},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data[0]["longShortRatio"]) if data else 1.0
    except Exception as e:
        logger.debug(f"多空比获取失败 {symbol}: {e}")
        return 1.0


def get_funding_rate(symbol: str) -> float:
    """获取当前资金费率"""
    try:
        resp = requests.get(
            f"{FAPI_BASE}/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=8,
        )
        resp.raise_for_status()
        return float(resp.json()["lastFundingRate"])
    except Exception as e:
        logger.debug(f"费率获取失败 {symbol}: {e}")
        return 0.0


def calc_volume_ratio(symbol: str) -> float:
    """量比：最近5分钟成交量 / 过去55分钟平均每5分钟成交量"""
    try:
        klines = get_klines_1m(symbol, 60)
        if len(klines) < 10:
            return 1.0
        recent_vol = sum(float(k[5]) for k in klines[-5:])
        older_vols  = [float(k[5]) for k in klines[:-5]]
        avg_5m_vol  = sum(older_vols) / len(older_vols) * 5 if older_vols else 1.0
        return round(recent_vol / avg_5m_vol, 2) if avg_5m_vol > 0 else 1.0
    except Exception as e:
        logger.debug(f"量比计算失败 {symbol}: {e}")
        return 1.0


def fetch_market_data(symbol: str, cfg: dict = None) -> dict:
    """拉取打分所需市场数据（含G因子公告状态）"""
    data = {
        "oi_change_pct":    get_oi_change(symbol),
        "long_short_ratio": get_lsr(symbol),
        "funding_rate":     get_funding_rate(symbol),
        "volume_ratio":     calc_volume_ratio(symbol),
    }
    try:
        from analyzer import is_delist_target
        if is_delist_target(symbol, cfg or CFG):
            data["announce_status"] = "delist"
    except Exception:
        pass
    return data


# ══════════════════════════════════════════
# 过滤器（ATH缓存）
# ══════════════════════════════════════════

_ath_cache: dict = {}


def _load_ath(symbol: str) -> dict:
    """加载并缓存ATH数据（上线天数+历史最高价+距ATH跌幅）"""
    if symbol in _ath_cache:
        return _ath_cache[symbol]
    try:
        klines = get_klines_1d(symbol)
        days = len(klines)
        ath  = max(float(k[2]) for k in klines)
        cur  = float(klines[-1][4])
        drop = round((1 - cur / ath) * 100, 1) if ath > 0 else 0
        _ath_cache[symbol] = {"listing_days": days, "ath": ath, "drop_from_ath": drop}
    except Exception as e:
        logger.warning(f"ATH加载失败 {symbol}: {e}")
        _ath_cache[symbol] = {"listing_days": 999, "ath": 0, "drop_from_ath": 50}
    return _ath_cache[symbol]


def passes_filter(symbol: str, ticker: dict) -> tuple[bool, str]:
    """
    基础过滤 v3.1
    返回 (True, "") 或 (False, 原因)
    """
    min_vol = CFG["exclude_daily_vol_below"]
    if ticker["volume_usdt"] < min_vol:
        return False, f"成交额{ticker['volume_usdt']/1e6:.1f}M<{min_vol/1e6:.0f}M"

    ath_data = _load_ath(symbol)
    listing_days = ath_data["listing_days"]
    if listing_days < CFG["min_listing_days"]:
        return False, f"上线{listing_days}天<{CFG['min_listing_days']}天"

    ath_exempt = CFG.get("ath_exempt_days", 180)
    if listing_days >= ath_exempt:
        min_drop = CFG["min_drop_from_ath"]
        if ath_data["drop_from_ath"] < min_drop:
            return False, f"距ATH跌{ath_data['drop_from_ath']:.1f}%<{min_drop}%"

    return True, ""


# 启动时黑名单（任意时段>15%排除，2小时后过期可重新进池）
_startup_blacklist: dict = {}  # {symbol: expire_ts}
_startup_done: bool = False
_BLACKLIST_TTL = 1800  # 30分钟（7200→1800）


def build_startup_blacklist(tickers: list):
    """启动时扫全市场，任意时段>15%加入黑名单（2小时过期）"""
    global _startup_blacklist, _startup_done
    if _startup_done:
        return
    expire_ts = time.time() + _BLACKLIST_TTL
    threshold = CFG.get("startup_max_change_pct", 15)
    for t in tickers:
        symbol = t["symbol"]
        ok, _ = passes_filter(symbol, t)
        if not ok:
            continue
        try:
            c1m = calc_1m_change(symbol)
            c3m = calc_3m_change(symbol)
            c5m = calc_5m_change(symbol)
            c15m = calc_15m_change(symbol)
            c1h = calc_1h_change(symbol)
            c2h = calc_2h_change(symbol)
            changes = [c1m, c3m, c5m, c15m, c1h, c2h]
            if any(c > threshold for c in changes):
                _startup_blacklist[symbol] = expire_ts
                logger.info(f"[启动黑名单] {symbol} 排除2h: 1m={c1m:.1f}% 3m={c3m:.1f}% 5m={c5m:.1f}% 15m={c15m:.1f}% 1h={c1h:.1f}% 2h={c2h:.1f}%")
        except Exception as e:
            logger.debug(f"[启动黑名单] {symbol} 检查失败: {e}")
    _startup_done = True
    logger.info(f"[启动黑名单] 排除{len(_startup_blacklist)}个币: {list(_startup_blacklist)[:20]}")


# ══════════════════════════════════════════
# 状态管理
# ══════════════════════════════════════════

STATE_FILE = DATA_DIR / "scanner_state.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "watchpool":  {},   # {symbol: {entered_at, entry_price, peak_price, cur_price, ...}}
        "signals":    [],   # 活跃信号（待结算）
        "signal_history": [],  # 已结算信号（成功/失败/过期）
        "positions":  {},   # {symbol: {entry_price, entry_time, order_id, score, ...}}
        "cooldowns":  {},   # {symbol: {expire_at, type, last_entry_price}}
        "filter_log": [],   # 过滤日志，滚动50条
        "stats": {"scans": 0, "pool_entries": 0, "signals": 0},
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


SCORE_HISTORY = Path(__file__).parent / "data" / "score_history.jsonl"


def _append_score_history(symbol: str, result: dict, market_data: dict):
    SCORE_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "action": result.get("action"),
        "score": result.get("score"),
        "breakdown": result.get("breakdown", {}),
        "change_1h": market_data.get("change_1h"),
        "change_5m": market_data.get("change_5m"),
        "change_1m": market_data.get("change_1m"),
        "vol_ratio": market_data.get("vol_ratio"),
        "oi_usdt": market_data.get("oi_usdt"),
    }
    with open(SCORE_HISTORY, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ══════════════════════════════════════════
# 核心扫描逻辑
# ══════════════════════════════════════════

def scan_once(state: dict, tickers: list) -> dict:
    """
    v3.1 扫描逻辑
    第一步：基础过滤 + 启动黑名单
    第二步：瞬时触发(任意一条) + OI≥500万 → 进观察池
    第三步：观察池持续评分
    """
    now = time.time()
    ticker_map = {t["symbol"]: t for t in tickers}
    events = {"new_pool": [], "new_signals": [], "pool_exits": []}

    state["stats"]["scans"] += 1

    # ── 1. 清理过期冷却（时间到期 OR 价格回落解除） ──
    price_release_pct = CFG.get("cooldown_price_release_pct", 80) / 100.0
    price_release_on_sl = CFG.get("cooldown_price_release_on_sl", False)
    live_prices = {t["symbol"]: float(t.get("lastPrice", 0)) for t in tickers}
    surviving_cooldowns = {}
    for s, cd in state["cooldowns"].items():
        if isinstance(cd, (int, float)):
            cd = {"expire_at": cd, "type": "unknown", "last_entry_price": 0}
        if now >= cd["expire_at"]:
            logger.info(f"[冷却] {s} 时间到期，解除")
            continue
        entry_p = cd.get("last_entry_price", 0)
        cd_type = cd.get("type", "unknown")
        if cd_type != "sl" or price_release_on_sl:
            cur_p = live_prices.get(s, 0)
            if entry_p > 0 and cur_p > 0 and cur_p <= entry_p * price_release_pct:
                logger.info(f"[冷却] {s} 价格回落解除 cur={cur_p:.4f} <= entry*{price_release_pct}={entry_p*price_release_pct:.4f}")
                continue
        surviving_cooldowns[s] = cd
    state["cooldowns"] = surviving_cooldowns

    # ── 2. 基础过滤 ──
    candidates = []
    for t in tickers:
        symbol = t["symbol"]

        if symbol in state["watchpool"] or symbol in state["cooldowns"]:
            continue
        if symbol in state.get("positions", {}):
            continue
        if any(s["symbol"] == symbol for s in state.get("signals", [])):
            continue

        if symbol in _startup_blacklist and _startup_blacklist[symbol] > now:
            continue

        ok, reason = passes_filter(symbol, t)
        if not ok:
            state.setdefault("filter_log", []).append({
                "symbol": symbol, "reason": reason,
                "time": datetime.now().strftime("%H:%M:%S"),
            })
            if len(state["filter_log"]) > 50:
                state["filter_log"] = state["filter_log"][-50:]
            continue

        candidates.append(t)

    # ── 3. 进池：瞬时触发(任一) + OI≥500万 ──
    burst_1m_th = CFG.get("pool_burst_1m", 12)
    burst_3m_th = CFG.get("pool_burst_3m", 8)
    burst_5m_th = CFG.get("pool_burst_5m", 7)
    burst_15m_th = CFG.get("pool_burst_15m", 12)
    min_oi = CFG.get("min_oi_usdt", 5000000)

    for t in candidates:
        symbol = t["symbol"]

        c1m = calc_1m_change(symbol)
        c3m = calc_3m_change(symbol)
        c5m = calc_5m_change(symbol)
        c15m = calc_15m_change(symbol)

        triggered = (c1m > burst_1m_th or c3m > burst_3m_th
                     or c5m > burst_5m_th or c15m > burst_15m_th)

        # 第五条：阶梯型慢涨（15m>6%且量比>1.5x）
        if not triggered and c15m > CFG.get("pool_stair_15m", 6):
            vr = calc_volume_ratio(symbol)
            if vr > CFG.get("pool_stair_vol_ratio", 1.5):
                triggered = True
                logger.info(f"[进池] {symbol} 阶梯触发: 15m={c15m:.1f}% 量比={vr:.1f}x")

        if not triggered:
            continue

        oi = get_oi_usdt(symbol)
        if oi < min_oi:
            logger.debug(f"[进池过滤] {symbol} OI {oi/1e6:.1f}M < {min_oi/1e6:.0f}M")
            continue

        # 24h已涨过滤（防拉完回抽进池）
        pct_24h = t.get("change_pct", 0)
        max_24h = CFG.get("max_24h_change_pct", 30)
        if pct_24h > max_24h:
            logger.info(f"[否决进池] {symbol} 24h已涨{pct_24h:.1f}% > {max_24h}%，跳过")
            continue

        max_pool = CFG.get("watchpool_max", 30)
        if len(state["watchpool"]) >= max_pool:
            weakest = min(state["watchpool"].items(),
                          key=lambda x: x[1].get("gain_since_entry", 0))
            del state["watchpool"][weakest[0]]
            logger.info(f"[观察池] 满，淘汰 {weakest[0]}")

        change_1h = calc_1h_change(symbol)
        ath_data = _load_ath(symbol)

        # 锁定进池时的量比基准（改动4）
        base_avg_vol = 0
        try:
            _klines = get_klines_1m(symbol, 60)
            if len(_klines) >= 10:
                _older = [float(k[5]) for k in _klines[:-5]]
                base_avg_vol = sum(_older) / len(_older) * 5 if _older else 0
        except Exception:
            pass

        state["watchpool"][symbol] = {
            "entered_at":    now,
            "entry_price":   t["price"],
            "peak_price":    t["price"],
            "cur_price":     t["price"],
            "change_1h":     change_1h,
            "oi_usdt":       oi,
            "volume_usdt":   t["volume_usdt"],
            "drop_from_ath": ath_data["drop_from_ath"],
            "analyzed":      False,
            "last_analyze_price": 0,
            "last_analyze_time":  0,
            "base_avg_vol":  base_avg_vol,
        }
        state["stats"]["pool_entries"] += 1
        events["new_pool"].append(symbol)

        get_oi_change(symbol)  # 预热OI缓存（改动3）

        trigger_info = f"1m={c1m:.1f}% 3m={c3m:.1f}% 5m={c5m:.1f}% 15m={c15m:.1f}%"
        logger.info(
            f"[进池] {symbol} {trigger_info} OI:{oi/1e6:.1f}M "
            f"1h+{change_1h:.1f}% 价格:{t['price']} 基准量:{base_avg_vol:.0f}"
        )

        try:
            send_pool_entry({
                "symbol": symbol,
                "change_5m": c5m,
                "volume_usdt": t["volume_usdt"],
                "entry_price": t["price"],
                "drop_from_ath": ath_data["drop_from_ath"],
            })
        except Exception as _e:
            logger.warning(f"进池推送失败 {symbol}: {_e}")

    # ── 4. 第三层：观察池管理（零成本观察，定期打分） ──
    # 冷却中的币踢出观察池
    for _s in list(state["watchpool"].keys()):
        if _s in state["cooldowns"] or _s in state.get("positions", {}):
            logger.info(f"[观察池] {_s} 冷却/持仓中，踢出")
            del state["watchpool"][_s]

    for symbol in list(state["watchpool"].keys()):
        pool = state["watchpool"][symbol]
        t = ticker_map.get(symbol)
        if not t:
            continue

        cur_price   = t["price"]
        entry_price = pool["entry_price"]
        elapsed_min = (now - pool["entered_at"]) / 60

        # 更新峰值和当前价
        if cur_price > pool.get("peak_price", entry_price):
            pool["peak_price"] = cur_price
        pool["cur_price"] = cur_price

        # 进池后涨幅（核心基准，替代1h）
        gain_since_entry = (cur_price - entry_price) / entry_price * 100
        pool["gain_since_entry"] = round(gain_since_entry, 2)

        # 1h涨幅仅供打分参考，不作为门槛
        change_1h = calc_1h_change(symbol)
        pool["change_1h"] = change_1h

        # ── 退出条件 ──
        _pool_upper = CFG.get("pool_exit_upper_pct", 20)
        _pool_lower = CFG.get("pool_exit_lower_pct", 5)

        # 涨幅超上限 → 不追高，退出
        if gain_since_entry > _pool_upper:
            _record_reject(symbol, "over_20", pool.get("last_score", 0),
                           cur_price, entry_price, gain_since_entry,
                           pool.get("last_breakdown", ""))
            del state["watchpool"][symbol]
            events["pool_exits"].append({"symbol": symbol, "reason": f"涨幅{gain_since_entry:.1f}%>{_pool_upper}%不追高"})
            logger.info(f"[退出] {symbol} 涨幅{gain_since_entry:.1f}%>{_pool_upper}%不追高")
            continue

        # 涨幅跌回下限 → 动力不足，退出
        if gain_since_entry < _pool_lower:
            _record_reject(symbol, "under_5", pool.get("last_score", 0),
                           cur_price, entry_price, gain_since_entry,
                           pool.get("last_breakdown", ""))
            del state["watchpool"][symbol]
            events["pool_exits"].append({"symbol": symbol, "reason": f"涨幅跌回{gain_since_entry:.1f}%<{_pool_lower}%动力不足"})
            logger.info(f"[退出] {symbol} 涨幅{gain_since_entry:.1f}%<{_pool_lower}%动力不足")
            continue

        # 峰值回撤>20% → 退出
        peak = pool.get("peak_price", entry_price)
        if peak > 0:
            drawdown = (peak - cur_price) / peak * 100
            if drawdown > 20:
                _record_reject(symbol, "peak_drawdown", pool.get("last_score", 0),
                               cur_price, entry_price, gain_since_entry,
                               pool.get("last_breakdown", ""))
                del state["watchpool"][symbol]
                events["pool_exits"].append({"symbol": symbol, "reason": f"峰值回撤{drawdown:.1f}%"})
                logger.info(f"[退出] {symbol} 峰值回撤{drawdown:.1f}%>20%")
                continue

        # ── 定期打分：价格变化≥1%就重新打分（无时间冷却） ──
        last_price = pool.get("last_analyze_price", 0)
        if last_price > 0 and abs(cur_price - last_price) / last_price * 100 >= 1:
            pool["analyzed"] = False

        if pool.get("analyzed"):
            continue

        # ── 打分（无前置门槛，直接进评分体系） ──
        analyze_result = None

        change_5m = calc_5m_change(symbol)
        change_1m = calc_1m_change(symbol)
        change_3m = calc_3m_change(symbol)
        market_data = fetch_market_data(symbol, CFG)
        market_data["change_1m"] = change_1m
        market_data["change_3m"] = change_3m
        market_data["change_5m"] = change_5m
        market_data["change_1h"] = change_1h

        # 用进池时锁定的量比基准替代滚动基准（改动4b）
        base_avg_vol = pool.get("base_avg_vol", 0)
        if base_avg_vol > 0:
            try:
                _recent = get_klines_1m(symbol, 5)
                _rvol = sum(float(k[5]) for k in _recent)
                market_data["volume_ratio"] = round(_rvol / base_avg_vol, 2)
            except Exception:
                pass

        logger.info(f"[分析] {symbol} 池内+{gain_since_entry:.1f}% 1h+{change_1h:.1f}% 5m+{change_5m:.1f}% 1m+{change_1m:.1f}%")
        analyze_result = analyze(symbol, gain_since_entry, market_data, cfg=CFG)
        if analyze_result:
            analyze_result["market_data"] = market_data

        if analyze_result is None:
            continue

        breakdown = analyze_result.get("breakdown", {})
        bd_str = " ".join(f"{k}={v}" for k, v in breakdown.items()) if breakdown else ""
        logger.info(
            f"[分析结果] {symbol}: {analyze_result['action']} "
            f"{analyze_result.get('reason', '')} "
            f"score={analyze_result.get('score', '-')} "
            f"breakdown=[{bd_str}]"
        )

        # ── 评分日志持久化（不受日志轮转影响） ──
        try:
            _append_score_history(symbol, analyze_result, market_data)
        except Exception:
            pass

        # ── 信号触发 ──
        if analyze_result["action"] == "signal_scored":
            # 冷却/持仓中的币不出信号
            if symbol in state["cooldowns"] or symbol in state.get("positions", {}):
                logger.info(f"[过滤] {symbol} 冷却/持仓中，跳过信号")
                continue
            # 24h涨幅过滤（不达标直接跳过，不记录信号、不推送、不显示）
            try:
                _ticker = requests.get(
                    f"{FAPI_BASE}/fapi/v1/ticker/24hr",
                    params={"symbol": symbol}, timeout=5,
                ).json()
                _pct_24h = float(_ticker.get("priceChangePercent", 0))
                _max_24h = CFG.get("max_24h_change_pct", 30)
                if _pct_24h > _max_24h:
                    _record_reject(symbol, "over_20", analyze_result.get("score", 0),
                                   cur_price, entry_price, gain_since_entry,
                                   str(analyze_result.get("breakdown", "")))
                    logger.info(f"[过滤] {symbol} 评分{analyze_result.get('score')}分但24h已涨{_pct_24h:.1f}%>{_max_24h}%，不记录不推送")
                    continue
            except Exception as _e:
                logger.warning(f"[24h检查异常] {symbol}: {_e}，保守跳过")
                continue

            signal = {
                "symbol":       symbol,
                "time":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "entry_price":  entry_price,
                "cur_price":    cur_price,
                "gain_pct":     round(gain_since_entry, 1),
                "volume_usdt":  t["volume_usdt"],
                "drop_from_ath": pool.get("drop_from_ath", 0),
                "elapsed_min":  round(elapsed_min, 1),
                "action":       analyze_result["action"],
                "reason":       analyze_result.get("reason", ""),
                "score":        analyze_result.get("score"),
                "ai_reason":    analyze_result.get("ai_reason", ""),
                "change_1h":    round(change_1h, 1),
                "oi_usdt":      pool.get("oi_usdt", 0),
                "vol_ratio":    pool.get("vol_ratio", 0),
            }
            state["signals"].append(signal)
            state["stats"]["signals"] += 1
            state["cooldowns"][symbol] = {
                "expire_at": now + CFG.get("cooldown_after_tp_hours", 12) * 3600,
                "type": "tp",
                "last_entry_price": signal.get("entry_price", 0),
            }
            del state["watchpool"][symbol]
            events["new_signals"].append(signal)

            logger.info(
                f"[信号] {symbol} 1h+{change_1h:.1f}% "
                f"原因:{signal['reason']} score={signal['score']}"
            )

            # 推送信号通知
            try:
                send_signal(signal)
            except Exception as e:
                logger.warning(f"信号推送失败 {symbol}: {e}")

            # 执行买入（下架反拉只推送不下单，SETTLING无法开仓）
            if analyze_result.get("reason") == "下架反拉":
                logger.info(f"[买入] {symbol} 下架反拉信号，只推送不下单")
            else:
                try:
                    buy_result = buyer_execute(
                        symbol=symbol,
                        price=cur_price,
                        analyze_result=analyze_result,
                        cfg=CFG,
                    )
                    logger.info(
                        f"[买入] {symbol}: {buy_result['status']} — {buy_result['reason']}"
                    )

                    # 买入成功 → 写入positions
                    if buy_result["status"] == "executed":
                        state.setdefault("positions", {})[symbol] = {
                            "entry_price": cur_price,
                            "entry_time": now,
                            "order_id": buy_result.get("order_id"),
                            "score": analyze_result.get("score"),
                            "breakdown": analyze_result.get("breakdown", {}),
                            "status": "holding",
                            "peak_pnl_pct": 0,
                            "sl_algo_id": buy_result.get("sl_algo_id"),
                            "tp_algo_id": buy_result.get("tp_order_id"),
                        }
                        logger.info(f"[仓位] {symbol} 已录入positions")

                    # 推送成交详情报告给乌鸦
                    try:
                        send_trade_report(signal, buy_result, analyze_result)
                    except Exception as e2:
                        logger.warning(f"成交报告推送失败 {symbol}: {e2}")
                except Exception as e:
                    logger.error(f"买入执行失败 {symbol}: {e}")

        # ── 利空否决 ──
        elif analyze_result["action"] == "veto":
            _record_reject(symbol, "veto", analyze_result.get("score", 0),
                           cur_price, entry_price, gain_since_entry,
                           str(analyze_result.get("breakdown", "")))
            del state["watchpool"][symbol]
            state["cooldowns"][symbol] = {
                "expire_at": now + CFG.get("cooldown_after_veto_min", 30) * 60,
                "type": "veto",
                "last_entry_price": pool.get("entry_price", 0),
            }
            events["pool_exits"].append({
                "symbol": symbol,
                "reason": f"否决: {analyze_result.get('reason', '')}",
            })
            logger.info(f"[否决] {symbol} {analyze_result.get('reason', '')}")

        # ── 继续观察 ──
        elif analyze_result["action"] == "hold":
            pool["analyzed"] = True
            pool["last_analyze_price"] = cur_price
            pool["last_analyze_time"] = now
            pool["last_score"] = analyze_result.get("score", 0)
            pool["last_breakdown"] = str(analyze_result.get("breakdown", ""))

    save_state(state)
    return events


# ══════════════════════════════════════════
# 信号生命周期结算
# ══════════════════════════════════════════

def _settle_signals(state: dict, now: float):
    """
    遍历活跃信号，按条件结算到 signal_history：
    - 涨幅 ≥ 50% → success
    - 跌幅 ≥ 30%（从入场价回撤） → failed
    - 超过 24h 未触发以上条件 → expired
    """
    if not state.get("signals"):
        return

    symbols = [s["symbol"] for s in state["signals"]]
    live_prices = {}
    try:
        resp = requests.get("https://fapi.binance.com/fapi/v1/ticker/price", timeout=8)
        resp.raise_for_status()
        live_prices = {t["symbol"]: float(t["price"]) for t in resp.json() if t["symbol"] in symbols}
    except Exception:
        return

    remaining = []
    for sig in state["signals"]:
        symbol = sig["symbol"]
        entry_price = sig.get("entry_price", 0)
        live_price = live_prices.get(symbol, 0)
        sig_time = sig.get("time", "")

        try:
            sig_ts = datetime.strptime(sig_time, "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            sig_ts = now

        elapsed_h = (now - sig_ts) / 3600
        settled = None

        if entry_price > 0 and live_price > 0:
            pnl_pct = (live_price - entry_price) / entry_price * 100
            if pnl_pct >= 50:
                settled = "success"
            elif pnl_pct <= -30:
                settled = "failed"

        if not settled and elapsed_h >= 24:
            settled = "expired"

        if settled:
            sig["status"] = settled
            sig["settled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sig["exit_price"] = live_price
            state.setdefault("signal_history", []).append(sig)
            if len(state["signal_history"]) > 50:
                state["signal_history"] = state["signal_history"][-50:]
            logger.info(f"[结算] {symbol} → {settled} 入场:{entry_price} 现价:{live_price}")
        else:
            remaining.append(sig)

    state["signals"] = remaining
    if len(remaining) < len(symbols):
        save_state(state)


# ══════════════════════════════════════════
# 主循环
# ══════════════════════════════════════════

def run():
    logger.info("=== 做多阻击扫描器 v3.1 启动 ===")
    state = load_state()
    last_full_scan     = 0
    last_health_report = 0
    last_card_hour     = -1
    last_health_hour   = -1

    while True:
        now = time.time()
        current_hour = _dt.datetime.now().hour

        # ── 配置热重载 ──
        _hot_reload_config()

        # ── 信号生命周期结算 ──
        _settle_signals(state, now)

        # ── 移动止盈检查（币安2仓位） ──
        if CFG.get("custom_trailing_enabled", False):
            try:
                from trailing_limit import check_all as tl_check
                from bull_trailing import _settle_signal
                tl_results = tl_check(CFG)
                for t in tl_results:
                    _settle_signal(state, t["symbol"], "success", 0)
                    logger.info(f"[限价止盈] {t['symbol']} 成交 盈亏+{t['pnl_pct']}%")
                if tl_results:
                    save_state(state)
            except Exception as e:
                logger.warning(f"[限价止盈] 检查异常: {e}")
        else:
            try:
                from bull_trailing import check_all as trailing_check
                tp_triggered = trailing_check()
                if tp_triggered:
                    for t in tp_triggered:
                        logger.info(f"[移动止盈] {t['symbol']} 触发 浮盈+{t['pnl_pct']}% 回撤-{t['drawdown']}%")
            except Exception as e:
                logger.warning(f"[移动止盈] 检查异常: {e}")

        # ── 仓位生命周期管理 ──
        try:
            from bull_trailing import check_positions
            if check_positions(state, CFG):
                save_state(state)
        except Exception as e:
            logger.warning(f"[仓位管理] 检查异常: {e}")

        # 每30秒全市场扫描
        if now - last_full_scan >= CFG.get("scan_interval_sec", 30):
            try:
                tickers = get_all_tickers()
                # 首次扫描：生成启动黑名单
                if not _startup_done:
                    logger.info(f"[启动黑名单] 开始扫描 {len(tickers)} 个合约...")
                    build_startup_blacklist(tickers)
                logger.info(f"全市场扫描: {len(tickers)}个合约")
                events = scan_once(state, tickers)

                # 更新拒绝追踪器峰值
                try:
                    _reject_prices = {t["symbol"]: float(t.get("lastPrice", 0)) for t in tickers}
                    _update_reject_peaks(_reject_prices)
                except Exception as _e:
                    logger.debug(f"[拒绝追踪] 更新异常: {_e}")

                if events["new_pool"]:
                    logger.info(f"新进池: {events['new_pool']}")
                if events["new_signals"]:
                    logger.info(f"新信号: {[s['symbol'] for s in events['new_signals']]}")

                last_full_scan = now
            except Exception as e:
                logger.error(f"扫描异常: {e}")

        # :00 整点：群组状态卡片（扫描后推送，确保反映最新状态）
        if current_hour != last_card_hour:
            try:
                send_status_card(state)
                last_card_hour = current_hour
                logger.info("[状态卡片] 已推群组")
            except Exception as e:
                logger.warning(f"状态卡片推送失败: {e}")

        # :02 健康报告（私信乌鸦，与状态卡错开）
        current_min = _dt.datetime.now().minute
        if current_min == 2 and current_hour != last_health_hour:
            try:
                state["_oi_cache"] = _oi_cache
                send_health_report(state, state.get("filter_log", []))
                state.pop("_oi_cache", None)
                state["stats"] = {"scans": 0, "pool_entries": 0, "signals": 0}
                last_health_report = now
                last_health_hour = current_hour
                logger.info("[健康报告] 已推送（:02）")
            except Exception as e:
                logger.warning(f"健康报告推送失败: {e}")

        # 观察池有币时，每10秒刷新
        elif state["watchpool"]:
            try:
                tickers = get_all_tickers()
                scan_once(state, tickers)
                try:
                    _reject_prices = {t["symbol"]: float(t.get("lastPrice", 0)) for t in tickers}
                    _update_reject_peaks(_reject_prices)
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"观察池刷新异常: {e}")

        # 睡眠
        if state["watchpool"]:
            time.sleep(CFG.get("watchpool_refresh_sec", 10))
        else:
            remaining = CFG.get("scan_interval_sec", 30) - (time.time() - last_full_scan)
            time.sleep(max(1, remaining))


if __name__ == "__main__":
    run()
