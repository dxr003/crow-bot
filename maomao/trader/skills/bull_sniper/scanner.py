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
import os
import queue
import sys
import threading
import time
import yaml
import requests
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

if "/root" not in sys.path:
    sys.path.insert(0, "/root")
from ledger import get_ledger, new_trace_id, set_trace_id

from analyzer import analyze
from notifier import send_signal, send_health_report, send_status_card, send_trade_report, send_pool_entry
from buyer import execute as buyer_execute
from reject_tracker import record_exit as _record_reject, update_peaks as _update_reject_peaks
from news_score import get_smart_money_score
from _atomic import atomic_write_json

# 通用前置过滤器注册表（config.yaml: pre_filters 列表按顺序串联，任一拒即剔）
from filters import exchange_info as _ei_filter
_PRE_FILTERS = {
    "exchange_info": _ei_filter,
}

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
        RotatingFileHandler(
            LOG_DIR / "scanner.log",
            maxBytes=20 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("bull_scanner")

# L0 事件账本：进池/退出/信号/买入 结构化落盘（/root/logs/signal/bull_sniper.jsonl）
_ledger = get_ledger("signal", "bull_sniper")


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

# 前置过滤拒入日志（jsonl，rolling）
_FILTER_LOG_PATH = DATA_DIR / "filter_log.jsonl"
_FILTER_LOG_MAX_BYTES = 5 * 1024 * 1024   # 5MB 后切片


def _log_filter_reject(symbol: str, filter_name: str, reason: str) -> None:
    """前置过滤拒入写 jsonl，超过 5MB 自动备份切片。"""
    try:
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "symbol": symbol,
            "filter": filter_name,
            "reason": reason,
        }
        if _FILTER_LOG_PATH.exists() and _FILTER_LOG_PATH.stat().st_size > _FILTER_LOG_MAX_BYTES:
            _FILTER_LOG_PATH.rename(_FILTER_LOG_PATH.with_suffix(".jsonl.1"))
        with _FILTER_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"[filter_log] 写入失败: {e}")


# ══════════════════════════════════════════
# 币安API
# ══════════════════════════════════════════


# ── 市值排名排除（CoinGecko） ──
_mcap_exclude: set = set()
_mcap_exclude_ts: float = 0

def _refresh_mcap_exclude() -> set:
    """CoinGecko拉市值前N名，1小时缓存"""
    global _mcap_exclude, _mcap_exclude_ts
    now = time.time()
    if _mcap_exclude and now - _mcap_exclude_ts < 86400:  # 24小时刷新一次
        return _mcap_exclude
    top_n = CFG.get("mcap_exclude_top", 50)
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "order": "market_cap_desc",
                    "per_page": top_n, "page": 1},
            timeout=10,
        )
        resp.raise_for_status()
        symbols = {c["symbol"].upper() + "USDT" for c in resp.json()}
        _mcap_exclude.clear()
        _mcap_exclude.update(symbols)
        _mcap_exclude_ts = now
        logger.info(f"[市值排除] 刷新完毕，排除前{top_n}名 ({len(symbols)}个)")
    except Exception as e:
        logger.warning(f"[市值排除] CoinGecko获取失败: {e}，使用旧缓存")
    return _mcap_exclude


def get_all_tickers() -> list:
    """获取全市场合约24h行情，过 config.yaml 的 pre_filters 链。"""
    resp = requests.get(f"{FAPI_BASE}/fapi/v1/ticker/24hr", timeout=10)
    resp.raise_for_status()
    pre_filters = CFG.get("pre_filters") or []
    tickers = []
    for t in resp.json():
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        # 通用前置过滤链（按 config.yaml 顺序，任一拒即剔，写 filter_log.jsonl）
        rejected = False
        for fname in pre_filters:
            f = _PRE_FILTERS.get(fname)
            if not f:
                continue
            ok, reason = f.is_tradeable(sym)
            if not ok:
                _log_filter_reject(sym, fname, reason)
                rejected = True
                break
        if rejected:
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


# ══════════════════════════════════════════
# RC 前置门辅助函数
# ══════════════════════════════════════════

_kline_cache: dict = {}  # {"{symbol}_{interval}": (klines, expire_ts)}


def _get_klines_cached(symbol: str, interval: str, limit: int, ttl_sec: int) -> list:
    key = f"{symbol}_{interval}"
    now = time.time()
    hit = _kline_cache.get(key)
    if hit and now < hit[1]:
        return hit[0]
    resp = requests.get(
        f"{FAPI_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    klines = resp.json()
    _kline_cache[key] = (klines, now + ttl_sec)
    return klines


def _calc_ema(prices: list, period: int) -> float:
    """EMA，返回最后一个值。数据不足时用 SMA 兜底。"""
    if len(prices) < period:
        return sum(prices) / len(prices)
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def rc2_pass(c1m: float, c3m: float, c5m: float, c15m: float,
             vol_ratio: float) -> tuple[bool, str]:
    """RC-2: 进池触发 — 瞬时爆发 or 慢涨侧门（满足任一即进池）"""
    b1  = CFG.get("rc2_burst_1m",  7)
    b3  = CFG.get("rc2_burst_3m",  5)
    b5  = CFG.get("rc2_burst_5m",  4)
    b15 = CFG.get("rc2_burst_15m", 8)
    s15 = CFG.get("rc2_stair_15m", 6)
    svr = CFG.get("rc2_stair_vol_ratio", 2.0)

    if c1m  > b1:  return True, f"burst_1m={c1m:.1f}%"
    if c3m  > b3:  return True, f"burst_3m={c3m:.1f}%"
    if c5m  > b5:  return True, f"burst_5m={c5m:.1f}%"
    if c15m > b15: return True, f"burst_15m={c15m:.1f}%"
    if c15m > s15 and vol_ratio > svr:
        return True, f"stair={c15m:.1f}%+vol={vol_ratio:.1f}x"
    return False, ""


def rc3_pass(symbol: str) -> tuple[bool, str]:
    """RC-3: 4H 趋势过滤 — 4H 收盘 > EMA20 才放行（缓存 60min）"""
    try:
        period = CFG.get("rc3_ema_period", 20)
        ttl    = CFG.get("rc3_cache_minutes", 60) * 60
        klines = _get_klines_cached(symbol, "4h", limit=period + 10, ttl_sec=ttl)
        if len(klines) < period:
            return True, ""  # 数据不足，fail-open
        closes = [float(k[4]) for k in klines]
        ema = _calc_ema(closes, period)
        cur = closes[-1]
        if cur <= ema:
            return False, f"RC-3: 4H收盘{cur:.4f}≤EMA{period}={ema:.4f}"
        return True, ""
    except Exception as e:
        logger.debug(f"[RC-3] {symbol} 跳过: {e}")
        return True, ""  # fail-open


def rc4_pass(symbol: str, ticker: dict) -> tuple[bool, str]:
    """RC-4: 反弹识别 — 大跌后假反弹过滤"""
    try:
        lookback  = CFG.get("rc4_lookback_hours", 4)
        max_dd    = CFG.get("rc4_max_drawdown_pct", 12) / 100
        threshold = CFG.get("rc4_rebound_threshold", 0.92)

        klines = _get_klines_cached(symbol, "1h", limit=lookback + 1, ttl_sec=300)
        if not klines:
            return True, ""
        max_high = max(float(k[2]) for k in klines)
        cur = ticker["price"]

        drawdown     = (max_high - cur) / max_high if max_high > 0 else 0
        is_rebounding = cur < max_high * threshold

        if drawdown > max_dd and is_rebounding:
            return False, (f"RC-4: 4H回撤{drawdown*100:.1f}% "
                           f"当前{cur:.4f}<高点{max_high:.4f}×{threshold}")
        return True, ""
    except Exception as e:
        logger.debug(f"[RC-4] {symbol} 跳过: {e}")
        return True, ""  # fail-open


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


def get_oi_change(symbol: str) -> float:
    """获取OI变化百分比（当前 vs N分钟前，v3.3改用历史API滑动窗口）"""
    period = CFG.get("oi_compare_period", "15m")
    try:
        resp = requests.get(
            f"{FAPI_BASE}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": period, "limit": 2},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        if len(data) < 2:
            return 0.0
        old_oi = float(data[0]["sumOpenInterestValue"])
        cur_oi = float(data[1]["sumOpenInterestValue"])
        if old_oi <= 0:
            return 0.0
        return round((cur_oi - old_oi) / old_oi * 100, 2)
    except Exception as e:
        logger.debug(f"OI历史获取失败 {symbol}: {e}")
        return 0.0


# ── 费率历史追踪（v3.2新增，计算波动幅度） ──
_funding_history: dict = {}  # {symbol: [{"rate": float, "time": float}, ...]}

def record_funding_rate(symbol: str, rate: float):
    """记录费率历史，保留1小时内数据"""
    now = time.time()
    if symbol not in _funding_history:
        _funding_history[symbol] = []
    _funding_history[symbol].append({"rate": rate, "time": now})
    # 清理1小时前的数据
    _funding_history[symbol] = [
        r for r in _funding_history[symbol] if now - r["time"] < 3600
    ]

def get_funding_swing(symbol: str) -> float:
    """获取1小时内费率波动幅度（最大值-最小值）"""
    history = _funding_history.get(symbol, [])
    if len(history) < 2:
        return 0.0
    rates = [r["rate"] for r in history]
    return max(rates) - min(rates)


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
    """量比 v2（2026-04-18）：最近5分钟主动买入额 / 过去55分钟平均每5分钟主动买入额
    k[10] = takerBuyQuoteAssetVolume（主动买入USDT额）
    砸盘时买方占比低，量比自然下降，避免冷币砸盘刷高量比。"""
    try:
        klines = get_klines_1m(symbol, 60)
        if len(klines) < 10:
            return 1.0
        recent_vol = sum(float(k[10]) for k in klines[-5:])
        older_vols  = [float(k[10]) for k in klines[:-5]]
        avg_5m_vol  = sum(older_vols) / len(older_vols) * 5 if older_vols else 1.0
        return round(recent_vol / avg_5m_vol, 2) if avg_5m_vol > 0 else 1.0
    except Exception as e:
        logger.debug(f"量比计算失败 {symbol}: {e}")
        return 1.0


def fetch_market_data(symbol: str, cfg: dict = None) -> dict:
    """拉取打分所需市场数据（含G因子公告状态+v3.2费率波动）

    C维度 v4.0（2026-04-19）：量比倍数 → 动能加速度+买占比
    - 拉60根1m K线，切分为 [最近2m, 2-5m区间(3根), 5-15m区间(10根)]
    - short_accel = (最近2m主动买入/分钟) / (2-5m主动买入/分钟)  闪电爆发检测
    - mid_accel   = (最近5m主动买入/分钟) / (5-15m主动买入/分钟) 阶梯慢涨检测
    - buy_ratio_5m = 最近5m k[10] / 最近5m k[7]                 砸盘过滤
    - back_2m_buy_usdt = 最近2m主动买入绝对额                   冷币过滤
    旧 volume_ratio/vol_5m_buy_usdt 字段保留供进池逻辑/卡片显示
    """
    funding = get_funding_rate(symbol)
    # 记录费率历史用于波动计算（v3.2）
    record_funding_rate(symbol, funding)

    # C维度 v4.0 采集
    vol_ratio = 1.0
    vol_5m_buy_usdt = 0.0
    back_2m_buy_usdt = 0.0
    short_accel = 1.0
    mid_accel = 1.0
    buy_ratio_5m = 0.0
    try:
        klines = get_klines_1m(symbol, 60)
        n = len(klines)
        if n >= 15:
            back2 = klines[-2:]          # 最近2分钟
            mid3  = klines[-5:-2]        # 2-5m 区间（3根）
            early10 = klines[-15:-5]     # 5-15m 区间（10根）
            # 主动买入额分段
            b2_buy  = sum(float(k[10]) for k in back2)
            m3_buy  = sum(float(k[10]) for k in mid3)
            e10_buy = sum(float(k[10]) for k in early10)
            # 短期加速度：最近2m平均 / 前3m平均（每分钟）
            if m3_buy > 0:
                short_accel = round((b2_buy / 2) / (m3_buy / 3), 2)
            elif b2_buy > 0:
                short_accel = 99.0  # 前3m完全无买入
            # 中期加速度：最近5m平均 / 前10m平均（每分钟）
            last5_buy = b2_buy + m3_buy
            if e10_buy > 0:
                mid_accel = round((last5_buy / 5) / (e10_buy / 10), 2)
            elif last5_buy > 0:
                mid_accel = 99.0
            back_2m_buy_usdt = round(b2_buy, 2)
            # 买占比（最近5m主动买入 / 最近5m总成交）
            last5_total = sum(float(k[7]) for k in klines[-5:])
            if last5_total > 0:
                buy_ratio_5m = round(last5_buy / last5_total, 3)
            # 旧 volume_ratio 保留（进池路径 + 卡片显示）
            older_buys  = [float(k[10]) for k in klines[:-5]]
            avg_5m_buy  = sum(older_buys) / len(older_buys) * 5 if older_buys else 1.0
            vol_ratio   = round(last5_buy / avg_5m_buy, 2) if avg_5m_buy > 0 else 1.0
            vol_5m_buy_usdt = round(last5_buy, 2)
        elif n >= 10:
            # 降级：只有10-14根K线时走旧逻辑
            last5_buy = sum(float(k[10]) for k in klines[-5:])
            older_buys  = [float(k[10]) for k in klines[:-5]]
            avg_5m_buy  = sum(older_buys) / len(older_buys) * 5 if older_buys else 1.0
            vol_ratio   = round(last5_buy / avg_5m_buy, 2) if avg_5m_buy > 0 else 1.0
            vol_5m_buy_usdt = round(last5_buy, 2)
            back_2m_buy_usdt = round(sum(float(k[10]) for k in klines[-2:]), 2)
            last5_total = sum(float(k[7]) for k in klines[-5:])
            if last5_total > 0:
                buy_ratio_5m = round(last5_buy / last5_total, 3)
    except Exception as e:
        logger.debug(f"C维度数据采集失败 {symbol}: {e}")
    data = {
        "oi_change_pct":    get_oi_change(symbol),
        "long_short_ratio": get_lsr(symbol),
        "funding_rate":     funding,
        "funding_swing":    get_funding_swing(symbol),
        "volume_ratio":     vol_ratio,
        "vol_5m_buy_usdt":  vol_5m_buy_usdt,
        # C维度 v4.0 新字段
        "back_2m_buy_usdt": back_2m_buy_usdt,
        "short_accel":      short_accel,
        "mid_accel":        mid_accel,
        "buy_ratio_5m":     buy_ratio_5m,
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


# OI 缓存（2026-04-18 v3.6：OI 上移到启动过滤后避免每轮扫全市场爆 API）
_oi_cache: dict = {}  # {symbol: (oi_usdt, timestamp)}
_OI_CACHE_TTL = 300   # 5 分钟


def _load_oi_cached(symbol: str) -> float:
    now = time.time()
    hit = _oi_cache.get(symbol)
    if hit and (now - hit[1]) < _OI_CACHE_TTL:
        return hit[0]
    oi = get_oi_usdt(symbol)
    _oi_cache[symbol] = (oi, now)
    return oi


def passes_filter(symbol: str, ticker: dict) -> tuple[bool, str]:
    """RC-1 基础门 v3.5: 成交额 + 上线天数 + OI"""
    min_vol = CFG.get("rc1_min_24h_volume_usdt", 20_000_000)
    if ticker["volume_usdt"] < min_vol:
        return False, f"成交额{ticker['volume_usdt']/1e6:.1f}M<{min_vol/1e6:.0f}M"

    ath_data = _load_ath(symbol)
    listing_days = ath_data["listing_days"]
    min_days = CFG.get("rc1_min_listing_days", 5)
    if listing_days < min_days:
        return False, f"上线{listing_days}天<{min_days}天"

    min_oi = CFG.get("rc1_min_oi_usdt", 5_000_000)
    oi = _load_oi_cached(symbol)
    if oi < min_oi:
        return False, f"OI{oi/1e6:.1f}M<{min_oi/1e6:.0f}M"

    return True, ""




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
    atomic_write_json(STATE_FILE, state)


# === State Schema & Sanitizer（防静默瘫痪）===
# 2026-04-18 signals字段被历史操作写成dict导致全天0成交，新增启动自检
_STATE_SCHEMA = {
    "watchpool":      dict,
    "signals":        list,
    "cooldowns":      dict,
    "filter_log":     list,
    "stats":          dict,
    "signal_history": list,
    "positions":      dict,
    "settled":        dict,
}

def _sanitize_state(state: dict) -> list:
    """启动时强校验state字段类型，错了自动修复并返回修复记录。"""
    fixes = []
    for key, expected in _STATE_SCHEMA.items():
        if key not in state:
            state[key] = expected()
            fixes.append(f"补字段 {key}={expected.__name__}()")
        elif not isinstance(state[key], expected):
            old = type(state[key]).__name__
            state[key] = expected()
            fixes.append(f"⚠️类型错 {key}:{old}→{expected.__name__}(已清空)")
    if "pool" in state:
        del state["pool"]
        fixes.append("清死字段 pool")
    return fixes


# === 异常告警（同类1h去重，防刷屏）===
_ALERT_DEDUP = {}
_ALERT_TTL = 3600

def _alert(key: str, msg: str):
    """scanner关键异常推TG给爸爸，同key 1h内只推一次。"""
    now = time.time()
    if now - _ALERT_DEDUP.get(key, 0) < _ALERT_TTL:
        return
    _ALERT_DEDUP[key] = now
    try:
        from notifier import _send_admin
        _send_admin(f"🚨 <b>bull-sniper异常</b>\n{msg}")
    except Exception as e:
        logger.warning(f"告警推送失败: {e}")


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
        "vol_ratio": market_data.get("volume_ratio"),
        "oi_change_pct": market_data.get("oi_change_pct"),
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
        if symbol in CFG.get("exclude_symbols", []):
            continue
        if symbol in _refresh_mcap_exclude():
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

    # ── 3. 进池：RC-2 → RC-4 → RC-3 ──
    for t in candidates:
        symbol = t["symbol"]

        c1m  = calc_1m_change(symbol)
        c3m  = calc_3m_change(symbol)
        c5m  = calc_5m_change(symbol)
        c15m = calc_15m_change(symbol)
        vr   = calc_volume_ratio(symbol)

        # RC-2: 瞬时爆发 or 慢涨侧门
        ok2, entry_reason = rc2_pass(c1m, c3m, c5m, c15m, vr)
        if not ok2:
            continue

        # RC-4: 反弹识别（便宜，先于 RC-3）
        ok4, reason4 = rc4_pass(symbol, t)
        if not ok4:
            logger.info(f"[RC-4拦截] {symbol} {reason4}")
            continue

        # RC-3: 4H EMA 趋势（最贵，放最后）
        ok3, reason3 = rc3_pass(symbol)
        if not ok3:
            logger.info(f"[RC-3拦截] {symbol} {reason3}")
            continue

        logger.info(f"[进池] {symbol} RC-2触发: {entry_reason} "
                    f"(1m={c1m:.1f}% 3m={c3m:.1f}% 5m={c5m:.1f}% 15m={c15m:.1f}% 量比={vr:.1f}x)")

        oi = _load_oi_cached(symbol)

        max_pool = CFG.get("watchpool_max", 30)
        if len(state["watchpool"]) >= max_pool:
            weakest = min(state["watchpool"].items(),
                          key=lambda x: x[1].get("gain_since_entry", 0))
            del state["watchpool"][weakest[0]]
            logger.info(f"[观察池] 满，淘汰 {weakest[0]}")

        change_1h = calc_1h_change(symbol)
        ath_data = _load_ath(symbol)

        # 锁定进池时的量比基准（v3.6 2026-04-18：k[5]→k[10] 主动买入额）
        base_avg_vol = 0
        try:
            _klines = get_klines_1m(symbol, 60)
            if len(_klines) >= 10:
                _older = [float(k[10]) for k in _klines[:-5]]
                base_avg_vol = sum(_older) / len(_older) * 5 if _older else 0
        except Exception:
            pass

        pool_tid = new_trace_id()
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
            "entry_reason":  entry_reason,
            "trace_id":      pool_tid,
        }
        state["stats"]["pool_entries"] += 1
        events["new_pool"].append(symbol)

        get_oi_change(symbol)  # 预热OI缓存（改动3）

        trigger_info = f"1m={c1m:.1f}% 3m={c3m:.1f}% 5m={c5m:.1f}% 15m={c15m:.1f}%"
        logger.info(
            f"[进池] {symbol} [{entry_reason}] {trigger_info} OI:{oi/1e6:.1f}M "
            f"1h+{change_1h:.1f}% 价格:{t['price']} 基准量:{base_avg_vol:.0f}"
        )
        _ledger.event("pool_entry", {
            "symbol": symbol,
            "entry_reason": entry_reason,
            "entry_price": t["price"],
            "change_1m": c1m, "change_3m": c3m, "change_5m": c5m, "change_15m": c15m,
            "oi_usdt": oi,
            "volume_usdt": t["volume_usdt"],
            "change_1h": change_1h,
            "base_avg_vol": base_avg_vol,
            "drop_from_ath": ath_data["drop_from_ath"],
        }, trace_id=pool_tid)

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

        # ── 退出条件：仅24小时超时 ──
        _pool_ttl_h = CFG.get("pool_ttl_hours", 24)
        if elapsed_min > _pool_ttl_h * 60:
            _record_reject(symbol, "timeout_24h", pool.get("last_score", 0),
                           cur_price, entry_price, gain_since_entry,
                           pool.get("last_breakdown", ""))
            _ledger.event("pool_exit", {
                "symbol": symbol,
                "reason": "timeout_24h",
                "elapsed_min": round(elapsed_min, 1),
                "ttl_hours": _pool_ttl_h,
                "last_score": pool.get("last_score", 0),
                "gain_since_entry": round(gain_since_entry, 2),
                "entry_price": entry_price,
                "exit_price": cur_price,
            }, trace_id=pool.get("trace_id", ""))
            del state["watchpool"][symbol]
            events["pool_exits"].append({"symbol": symbol, "reason": f"观察{_pool_ttl_h}h未触发信号，超时退出"})
            logger.info(f"[退出] {symbol} 观察{elapsed_min:.0f}分钟>{_pool_ttl_h}h，超时退出")
            continue

        # ── 池冻结机制 v4.0（2026-04-19 修死锁）──
        # 池内涨幅>freeze阈值：冻结评分，不推信号，继续跟踪价格
        # 回落到≤freeze阈值：立刻解冻
        # v3.4的双阈值缓冲带有死锁bug——币从25%回落到15%既不解冻也不再冻结，永远停在冻结态
        # 评分本身已有"1%价格变化才重评"的冷却，不需要额外缓冲带
        _freeze_gain = CFG.get("pool_freeze_gain_pct", 20)
        if gain_since_entry > _freeze_gain:
            if not pool.get("frozen"):
                pool["frozen"] = True
                logger.info(f"[冻结] {symbol} 池内+{gain_since_entry:.1f}%>{_freeze_gain}%，冻结评分")
            continue
        elif pool.get("frozen"):
            pool["frozen"] = False
            pool["analyzed"] = False  # 强制重新评分
            logger.info(f"[解冻] {symbol} 池内+{gain_since_entry:.1f}%≤{_freeze_gain}%，恢复评分")

        # 更新峰值（保留追踪，不作为退出条件）
        peak = pool.get("peak_price", entry_price)

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
        change_15m = calc_15m_change(symbol)
        market_data = fetch_market_data(symbol, CFG)
        market_data["change_1m"] = change_1m
        market_data["change_3m"] = change_3m
        market_data["change_5m"] = change_5m
        market_data["change_15m"] = change_15m
        market_data["change_1h"] = change_1h

        # 用进池时锁定的量比基准替代滚动基准（v3.6 2026-04-18：k[5]→k[10] 主动买入额）
        base_avg_vol = pool.get("base_avg_vol", 0)
        if base_avg_vol > 0:
            try:
                _recent = get_klines_1m(symbol, 5)
                _rvol = sum(float(k[10]) for k in _recent)
                market_data["volume_ratio"] = round(_rvol / base_avg_vol, 2)
                # 同时更新 vol_5m_buy_usdt，保持 analyzer C 维度封顶判断一致
                market_data["vol_5m_buy_usdt"] = round(_rvol, 2)
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
            # ── 回落过滤器 v2.0（2026-04-18 量比绝对值门槛上线后简化）──
            # 只保留 1m 翻阴判断。peak_drop 因 C 维度绝对买入额封顶已变多余，移除。
            _reject_1m = CFG.get("signal_reject_1m_below", -1)
            if change_1m < _reject_1m:
                _reason = f"1m{change_1m:+.1f}%<{_reject_1m}%"
                logger.info(f"[回落过滤] {symbol} 评分{analyze_result.get('score')}分过线但{_reason}，本轮跳过")
                try:
                    from notifier import _send_admin
                    _send_admin(f"⏸️ <b>{symbol.replace('USDT','')}</b> 评分{analyze_result.get('score')}分过线但{_reason}，本轮跳过")
                except Exception:
                    pass
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
                "oi_change_pct": market_data.get("oi_change_pct", 0),
                "volume_ratio":  market_data.get("volume_ratio", 0),
            }
            signal_tid = pool.get("trace_id") or new_trace_id()
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
            _ledger.event("signal", {
                "symbol": symbol,
                "action": signal["action"],
                "reason": signal["reason"],
                "score": signal["score"],
                "entry_price": signal["entry_price"],
                "cur_price": signal["cur_price"],
                "gain_pct": signal["gain_pct"],
                "change_1h": signal["change_1h"],
                "oi_change_pct": signal["oi_change_pct"],
                "volume_ratio": signal["volume_ratio"],
                "elapsed_min": signal["elapsed_min"],
                "ai_reason": signal["ai_reason"],
            }, trace_id=signal_tid)

            # 推送信号通知
            try:
                send_signal(signal)
            except Exception as e:
                logger.warning(f"信号推送失败 {symbol}: {e}")

            # 执行买入（下架反拉只推送不下单，SETTLING无法开仓）
            if analyze_result.get("reason") == "下架反拉":
                logger.info(f"[买入] {symbol} 下架反拉信号，只推送不下单")
                _ledger.event("buy_skipped", {
                    "symbol": symbol,
                    "reason": "delisting_rebound_no_trade",
                }, trace_id=signal_tid)
            else:
                # 把 signal trace_id 通过 ContextVar 传给 executor，
                # executor 那边 @log_call 会继承为 parent-child 同链。
                set_trace_id(signal_tid)
                try:
                    buy_result = buyer_execute(
                        symbol=symbol,
                        price=cur_price,
                        analyze_result=analyze_result,
                        cfg=CFG,
                    )

                    # 记录各账户执行结果
                    acct_results = buy_result.get("_accounts", {})
                    if acct_results:
                        for _an, _ar in acct_results.items():
                            logger.info(f"[买入] {symbol} [{_an}]: {_ar['status']} — {_ar['reason']}")
                    else:
                        logger.info(
                            f"[买入] {symbol}: {buy_result['status']} — {buy_result['reason']}"
                        )

                    # 买入成功 → 写入positions（任一账户成功即录入）
                    if buy_result["status"] == "executed":
                        # 收集成功执行的账户列表
                        executed_accts = [n for n, r in acct_results.items() if r.get("status") == "executed"] if acct_results else []
                        actual_open_price = buy_result.get("actual_entry") or cur_price
                        state.setdefault("positions", {})[symbol] = {
                            "entry_price": cur_price,
                            "entry_time": now,
                            # Stage 5.2: 止损分级 / trail 基准字段
                            "position_open_price": actual_open_price,
                            "position_open_time": now,
                            "entry_24h_change_pct": t.get("change_pct", 0),
                            "sl_upgraded": False,
                            "sl_pct": None,
                            # ──────────────────────────────
                            "order_id": buy_result.get("order_id"),
                            "score": analyze_result.get("score"),
                            "breakdown": analyze_result.get("breakdown", {}),
                            "status": "holding",
                            "peak_pnl_pct": 0,
                            "sl_algo_id": buy_result.get("sl_algo_id"),
                            "tp_algo_id": buy_result.get("tp_order_id"),
                            "accounts": executed_accts,
                            "trace_id": signal_tid,
                        }
                        logger.info(f"[仓位] {symbol} 已录入positions (账户: {', '.join(executed_accts) if executed_accts else '默认'})")

                    _ledger.event("buy_result", {
                        "symbol": symbol,
                        "status": buy_result.get("status"),
                        "reason": buy_result.get("reason"),
                        "order_id": buy_result.get("order_id"),
                        "entry_price": cur_price,
                        "accounts": {
                            n: {"status": r.get("status"), "reason": r.get("reason")}
                            for n, r in (acct_results or {}).items()
                        } if acct_results else {},
                    }, trace_id=signal_tid)

                    # 推送成交详情报告给乌鸦
                    try:
                        send_trade_report(signal, buy_result, analyze_result)
                    except Exception as e2:
                        logger.warning(f"成交报告推送失败 {symbol}: {e2}")
                except Exception as e:
                    logger.error(f"买入执行失败 {symbol}: {e}")
                    _ledger.event("buy_exception", {
                        "symbol": symbol,
                        "error": str(e),
                        "exception_type": type(e).__name__,
                    }, trace_id=signal_tid, level="ERROR")

        # ── 利空否决 ──
        elif analyze_result["action"] == "veto":
            _record_reject(symbol, "veto", analyze_result.get("score", 0),
                           cur_price, entry_price, gain_since_entry,
                           str(analyze_result.get("breakdown", "")))
            del state["watchpool"][symbol]
            # 不设冷却，退出后仍可重新进池
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
    遍历活跃信号兜底结算：
    - 已进 positions 的信号交给 bull_trailing 真实结算，这里跳过
    - mode=off（纯观察）才做 50%/-30% 虚拟判定；带 is_virtual=True
    - 任何 mode 下，超过 24h 未结算都打 expired 兜底
    """
    if not state.get("signals"):
        return

    mode = str(CFG.get("mode", "off")).lower()
    positions = state.get("positions", {})

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

        # 已进真实持仓 → 由 bull_trailing 结算，scanner 不碰
        if symbol in positions:
            remaining.append(sig)
            continue

        entry_price = sig.get("entry_price", 0)
        live_price = live_prices.get(symbol, 0)
        sig_time = sig.get("time", "")

        try:
            sig_ts = datetime.strptime(sig_time, "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            sig_ts = now

        elapsed_h = (now - sig_ts) / 3600
        settled = None

        # 虚拟成绩：仅 mode=off 下用价格涨跌判定
        if mode == "off" and entry_price > 0 and live_price > 0:
            pnl_pct = (live_price - entry_price) / entry_price * 100
            if pnl_pct >= 50:
                settled = "success"
            elif pnl_pct <= -30:
                settled = "failed"

        # 24h 兜底
        if not settled and elapsed_h >= 24:
            settled = "expired"

        if settled:
            sig["status"] = settled
            sig["settled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sig["exit_price"] = live_price
            sig["is_virtual"] = True  # scanner 兜底结算 = 虚拟成绩
            state.setdefault("signal_history", []).append(sig)
            if len(state["signal_history"]) > 50:
                state["signal_history"] = state["signal_history"][-50:]
            # 虚拟结算后写冷却，避免刚平就被海选回来（与 bull_trailing 真实结算路径对齐）
            cd_h = {"success": 12, "failed": 24, "expired": 6}.get(settled, 6)
            state.setdefault("cooldowns", {})[symbol] = {
                "expire_at": now + cd_h * 3600,
                "type": f"v{settled}",
                "last_entry_price": entry_price,
            }
            logger.info(f"[虚拟结算] {symbol} → {settled} 入场:{entry_price} 现价:{live_price} 冷却{cd_h}h")
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
    # 启动自检：防止state字段类型错导致静默瘫痪
    fixes = _sanitize_state(state)
    if fixes:
        for f in fixes:
            logger.warning(f"[启动自检] {f}")
        save_state(state)
        _alert("state_sanitize", "state启动自检修复:\n" + "\n".join(fixes))
    last_full_scan     = 0
    last_health_report = 0
    last_card_hour     = -1
    last_health_hour   = -1

    # ── Stage 5.3 两层移动止盈线程 ──
    _trail_queue: queue.Queue = queue.Queue()
    _trail_key    = os.environ.get("BINANCE2_API_KEY", "")
    _trail_secret = os.environ.get("BINANCE2_API_SECRET", "")
    if _trail_key and _trail_secret:
        try:
            from trail_manager import trail_loop
            _t = threading.Thread(
                target=trail_loop,
                args=(state, lambda: CFG, _trail_queue, _trail_key, _trail_secret),
                kwargs={"interval": CFG.get("trail_poll_interval_sec", 10)},
                daemon=True,
                name="trail_manager",
            )
            _t.start()
            logger.info("[trail] 移动止盈线程已启动（10s轮询）")
        except Exception as e:
            logger.warning(f"[trail] 线程启动失败: {e}")
    else:
        logger.warning("[trail] BINANCE2 密钥缺失，移动止盈线程未启动")

    while True:
        now = time.time()
        current_hour = _dt.datetime.now().hour

        # ── Stage 5.3 trail 队列消费（主线程独占写 state）──
        try:
            _trail_changed = False
            while not _trail_queue.empty():
                _item = _trail_queue.get_nowait()
                _sym = _item.get("symbol", "")
                _pos = state.get("positions", {}).get(_sym)
                if not _pos:
                    continue
                if _item["type"] == "update_peak":
                    _pos["peak_pnl_pct"] = _item["peak_pnl_pct"]
                    _trail_changed = True
                elif _item["type"] == "close" and _pos.get("status") == "holding":
                    from trail_manager import _close_position
                    _qty = _item.get("qty", 0)
                    _layer = _item.get("layer", "?")
                    _ok = _close_position(_sym, _qty, _trail_key, _trail_secret) if _qty > 0 else False
                    if _ok:
                        _pos["status"] = "trail_tp"
                        _pos["exit_price"] = _item.get("mark_price", 0)
                        logger.info(
                            f"[trail] {_sym} {_layer} 平仓成功 "
                            f"峰值+{_item['peak_pnl_pct']:.1f}% 现+{_item['cur_pnl_pct']:.1f}%"
                        )
                        _alert("trail_tp",
                            f"🏁 {_sym.replace('USDT','')} {_layer} 移动止盈触发\n"
                            f"峰值 +{_item['peak_pnl_pct']:.1f}% → 回撤至 +{_item['cur_pnl_pct']:.1f}%"
                        )
                    else:
                        logger.warning(f"[trail] {_sym} {_layer} 平仓失败，等下轮重试")
                    _trail_changed = True
            if _trail_changed:
                save_state(state)
        except Exception as e:
            logger.warning(f"[trail消费] 异常: {e}")

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
                    # tp→success / sl→failed，由 trailing_limit 判定
                    _status = t.get("status", "tp")
                    _exit_price = t.get("exit_price", 0)
                    _settle_signal(state, t["symbol"], _status, _exit_price)
                    logger.info(f"[限价止盈] {t['symbol']} 成交 盈亏{t['pnl_pct']:+.1f}% 状态:{_status}")
                if tl_results:
                    save_state(state)
            except Exception as e:
                logger.warning(f"[限价止盈] 检查异常: {e}")
        else:
            try:
                from bull_trailing import check_all as trailing_check
                tp_triggered = trailing_check(CFG)
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

        # ── Stage 5.2 止损升级（30分钟后宽松档位） ──
        try:
            from stop_loss_manager import upgrade_all_positions
            _sl_key    = os.environ.get("BINANCE2_API_KEY", "")
            _sl_secret = os.environ.get("BINANCE2_API_SECRET", "")
            if _sl_key and _sl_secret:
                upgraded = upgrade_all_positions(state, CFG, _sl_key, _sl_secret)
                if upgraded:
                    save_state(state)
        except Exception as e:
            logger.warning(f"[止损升级] 检查异常: {e}")

        # 每30秒全市场扫描
        if now - last_full_scan >= CFG.get("scan_interval_sec", 30):
            try:
                tickers = get_all_tickers()
                logger.info(f"全市场扫描: {len(tickers)}个合约")
                events = scan_once(state, tickers)

                # 更新拒绝追踪器峰值
                try:
                    _reject_prices = {t["symbol"]: float(t.get("price", 0)) for t in tickers}
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
                _alert("scan_exception", f"扫描异常: {e}")

        # 整点推群组状态卡片（2026-04-18 30min→1h，爸爸要求准点一次）
        current_min = _dt.datetime.now().minute
        if current_hour != last_card_hour and current_min < 5:
            try:
                send_status_card(state)
                last_card_hour = current_hour
                logger.info("[状态卡片] 已推群组")
            except Exception as e:
                logger.warning(f"状态卡片推送失败: {e}")

        # 每 4 小时 :02 健康报告（私信乌鸦，与状态卡错开；触发时刻 00/04/08/12/16/20）
        current_min = _dt.datetime.now().minute
        if current_hour % 4 == 0 and current_min == 2 and current_hour != last_health_hour:
            try:
                send_health_report(state, state.get("filter_log", []))
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
                    _reject_prices = {t["symbol"]: float(t.get("price", 0)) for t in tickers}
                    _update_reject_peaks(_reject_prices)
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"观察池刷新异常: {e}")
                _alert("pool_refresh_exception", f"观察池刷新异常: {e}")

        # 睡眠
        if state["watchpool"]:
            time.sleep(CFG.get("watchpool_refresh_sec", 10))
        else:
            remaining = CFG.get("scan_interval_sec", 30) - (time.time() - last_full_scan)
            time.sleep(max(1, remaining))


if __name__ == "__main__":
    run()
