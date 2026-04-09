#!/usr/bin/env python3
"""
bull_sniper scanner.py — 做多阻击扫描器（第一阶段：纯记录不开仓）

双层触发机制：
  1. 雷达预警：每30秒扫全市场，1分钟涨≥3% 标记为预观察
  2. 确认锁定：预观察币3分钟内累计涨≥5% 正式进入观察池

观察池：
  - 最多10个币，超出淘汰最早的
  - 每10秒刷新数据
  - 涨到10-18% 记录信号（第一阶段只记录不开仓）
  - 超18%/回落<3%/超时30分钟 自动退出

过滤条件：
  - 上线<30天排除
  - 24h涨幅已>30%排除
  - 日成交额<500万U排除
  - 距历史高点跌幅<30%排除
"""
import os
import sys
import json
import time
import logging
import yaml
import requests
from pathlib import Path
from datetime import datetime
from notifier import send_signal, send_pool_entry

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR = BASE_DIR / "logs"
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

# ── 配置 ──
def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", "r") as f:
        return yaml.safe_load(f)["bull_sniper"]

CFG = load_config()

# ── Binance API ──
FAPI_BASE = "https://fapi.binance.com"

def get_all_tickers() -> list:
    """获取全市场合约24h行情"""
    resp = requests.get(f"{FAPI_BASE}/fapi/v1/ticker/24hr", timeout=10)
    resp.raise_for_status()
    tickers = []
    for t in resp.json():
        if not t["symbol"].endswith("USDT"):
            continue
        try:
            tickers.append({
                "symbol": t["symbol"],
                "price": float(t["lastPrice"]),
                "change_pct": float(t["priceChangePercent"]),
                "volume_usdt": float(t["quoteVolume"]),
            })
        except (ValueError, KeyError):
            continue
    return tickers


def get_klines_1m(symbol: str, limit: int = 5) -> list:
    """获取1分钟K线"""
    resp = requests.get(
        f"{FAPI_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": "1m", "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_klines_1d(symbol: str) -> list:
    """获取日线K线（币安最大1500根，覆盖上线以来全部历史）"""
    resp = requests.get(
        f"{FAPI_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": "1d", "limit": 1500},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_exchange_info(symbol: str) -> dict:
    """获取合约信息（上线时间等）"""
    resp = requests.get(
        f"{FAPI_BASE}/fapi/v1/exchangeInfo",
        timeout=10,
    )
    resp.raise_for_status()
    for s in resp.json().get("symbols", []):
        if s["symbol"] == symbol:
            return s
    return {}


# ── 状态管理 ──
STATE_FILE = DATA_DIR / "scanner_state.json"

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "radar": {},          # 预观察: {symbol: {first_seen, first_price, ...}}
        "watchpool": {},      # 观察池: {symbol: {entered_at, entry_price, ...}}
        "signals": [],        # 记录的信号（第一阶段只记录）
        "cooldowns": {},      # 冷却: {symbol: expire_ts}
        "stats": {"scans": 0, "radar_hits": 0, "pool_entries": 0, "signals": 0},
    }

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ── 过滤器 ──
_exchange_info_cache = {}
_ath_cache = {}

def _get_listing_days(symbol: str) -> int:
    """估算上线天数（通过日线K线数量）"""
    if symbol in _ath_cache:
        return _ath_cache[symbol].get("listing_days", 999)
    try:
        klines = get_klines_1d(symbol)
        days = len(klines)
        ath = max(float(k[2]) for k in klines)  # k[2] = high
        cur = float(klines[-1][4])               # k[4] = close
        drop_from_ath = round((1 - cur / ath) * 100, 1) if ath > 0 else 0
        _ath_cache[symbol] = {
            "listing_days": days,
            "ath": ath,
            "drop_from_ath": drop_from_ath,
        }
        return days
    except Exception as e:
        logger.warning(f"获取 {symbol} 日线失败: {e}")
        return 999


def _get_drop_from_ath(symbol: str) -> float:
    """获取距历史高点的跌幅百分比"""
    if symbol not in _ath_cache:
        _get_listing_days(symbol)
    return _ath_cache.get(symbol, {}).get("drop_from_ath", 50)


def passes_filter(symbol: str, ticker: dict) -> tuple:
    """
    过滤检查，返回 (pass: bool, reason: str)
    """
    # 24h涨幅已超30%
    if ticker["change_pct"] > CFG["exclude_24h_above"]:
        return False, f"24h涨幅已{ticker['change_pct']:.1f}%>30%"

    # 日成交额<500万U
    if ticker["volume_usdt"] < CFG["exclude_daily_vol_below"]:
        return False, f"成交额{ticker['volume_usdt']/1e6:.1f}M<5M"

    # 上线天数<30
    listing_days = _get_listing_days(symbol)
    if listing_days < CFG["min_listing_days"]:
        return False, f"上线{listing_days}天<30天"

    # 距历史高点跌幅<30%
    drop = _get_drop_from_ath(symbol)
    if drop < CFG["min_drop_from_ath"]:
        return False, f"距高点跌{drop:.1f}%<30%"

    return True, ""


def calc_1m_change(symbol: str) -> float:
    """计算最近1分钟涨幅"""
    try:
        klines = get_klines_1m(symbol, 2)
        if len(klines) < 2:
            return 0
        prev_close = float(klines[-2][4])
        cur_close = float(klines[-1][4])
        if prev_close <= 0:
            return 0
        return round((cur_close - prev_close) / prev_close * 100, 2)
    except Exception:
        return 0


def calc_5m_change(symbol: str) -> float:
    """计算最近5分钟涨幅"""
    try:
        klines = get_klines_1m(symbol, 6)
        if len(klines) < 6:
            return 0
        prev_close = float(klines[-6][4])
        cur_close = float(klines[-1][4])
        if prev_close <= 0:
            return 0
        return round((cur_close - prev_close) / prev_close * 100, 2)
    except Exception:
        return 0


# ── 核心循环 ──

def scan_once(state: dict, tickers: list) -> dict:
    """执行一次完整扫描"""
    now = time.time()
    ticker_map = {t["symbol"]: t for t in tickers}
    events = {"new_radar": [], "new_pool": [], "new_signals": [], "pool_exits": []}

    state["stats"]["scans"] += 1

    # ── 1. 清理过期冷却 ──
    state["cooldowns"] = {s: t for s, t in state["cooldowns"].items() if t > now}

    # ── 2. 雷达预警：1分钟涨≥3% ──
    for t in tickers:
        symbol = t["symbol"]
        if symbol in state["watchpool"] or symbol in state["radar"]:
            continue
        if symbol in state["cooldowns"]:
            continue

        change_1m = calc_1m_change(symbol)
        if change_1m >= CFG["radar_1m_change"]:
            # 过滤检查
            ok, reason = passes_filter(symbol, t)
            if not ok:
                logger.debug(f"[雷达] {symbol} 1m+{change_1m}% 被过滤: {reason}")
                continue

            state["radar"][symbol] = {
                "first_seen": now,
                "first_price": t["price"],
                "change_1m": change_1m,
                "volume_usdt": t["volume_usdt"],
            }
            state["stats"]["radar_hits"] += 1
            events["new_radar"].append(symbol)
            logger.info(f"[雷达] {symbol} 1m涨+{change_1m}% 进入预观察")

    # ── 3. 预观察 → 确认进池：3分钟内5分钟涨≥5% ──
    for symbol in list(state["radar"].keys()):
        radar = state["radar"][symbol]
        elapsed = now - radar["first_seen"]

        # 超过3分钟没确认，淘汰
        if elapsed > 180:
            del state["radar"][symbol]
            logger.debug(f"[雷达] {symbol} 3分钟未确认，淘汰")
            continue

        change_5m = calc_5m_change(symbol)
        if change_5m >= CFG["confirm_5m_change"]:
            # 进入观察池
            t = ticker_map.get(symbol)
            if not t:
                continue

            # 观察池已满，淘汰最早的
            if len(state["watchpool"]) >= CFG["watchpool_max"]:
                oldest = min(state["watchpool"].items(), key=lambda x: x[1]["entered_at"])
                del state["watchpool"][oldest[0]]
                logger.info(f"[观察池] 已满，淘汰 {oldest[0]}")

            state["watchpool"][symbol] = {
                "entered_at": now,
                "entry_price": t["price"],
                "peak_price": t["price"],
                "change_5m": change_5m,
                "change_pct": t["change_pct"],
                "volume_usdt": t["volume_usdt"],
                "drop_from_ath": _get_drop_from_ath(symbol),
            }
            del state["radar"][symbol]
            state["stats"]["pool_entries"] += 1
            events["new_pool"].append(symbol)
            logger.info(f"[观察池] {symbol} 5m涨+{change_5m}% 正式进入")

            # 推送进池通知
            try:
                send_pool_entry(state["watchpool"][symbol])
            except Exception as e:
                logger.warning(f"进池推送失败: {e}")

    # ── 4. 观察池管理 ──
    for symbol in list(state["watchpool"].keys()):
        pool = state["watchpool"][symbol]
        t = ticker_map.get(symbol)
        if not t:
            continue

        cur_price = t["price"]
        entry_price = pool["entry_price"]
        elapsed_min = (now - pool["entered_at"]) / 60

        # 更新峰值
        if cur_price > pool.get("peak_price", entry_price):
            pool["peak_price"] = cur_price
        pool["cur_price"] = cur_price

        # 从进池价算涨幅
        gain_pct = (cur_price - entry_price) / entry_price * 100 if entry_price > 0 else 0

        # 退出条件
        exit_reason = None
        if gain_pct < 3:
            exit_reason = "涨幅回落<3%"
        elif gain_pct > CFG["snipe_window_max"]:
            exit_reason = f"超过{CFG['snipe_window_max']}%不追高"
        elif elapsed_min > CFG["watchpool_timeout_min"]:
            exit_reason = "观察超时30分钟"

        if exit_reason:
            del state["watchpool"][symbol]
            events["pool_exits"].append({"symbol": symbol, "reason": exit_reason})
            logger.info(f"[观察池] {symbol} 退出: {exit_reason}")
            continue

        # 信号触发：涨幅达到狙击窗口
        if gain_pct >= CFG["snipe_window_min"]:
            signal = {
                "symbol": symbol,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "entry_price": entry_price,
                "cur_price": cur_price,
                "gain_pct": round(gain_pct, 1),
                "volume_usdt": t["volume_usdt"],
                "drop_from_ath": pool.get("drop_from_ath", 0),
                "elapsed_min": round(elapsed_min, 1),
            }
            state["signals"].append(signal)
            state["stats"]["signals"] += 1

            # 第一阶段：只记录，不开仓，不移出观察池
            # 记录后设置冷却，避免重复触发
            state["cooldowns"][symbol] = now + 4 * 3600
            del state["watchpool"][symbol]
            events["new_signals"].append(signal)
            logger.info(
                f"[信号] {symbol} 涨+{gain_pct:.1f}% "
                f"入场价:{entry_price} 现价:{cur_price} "
                f"距高点跌{pool.get('drop_from_ath', 0):.1f}% "
                f"（第一阶段仅记录）"
            )

            # 推送信号通知（附带观察池快照）
            try:
                pool_snapshot = []
                for ps, pv in state["watchpool"].items():
                    ep = pv["entry_price"]
                    cp = pv.get("cur_price", ep)
                    pp = pv.get("peak_price", ep)
                    pool_snapshot.append({
                        "symbol": ps,
                        "entry_price": ep,
                        "cur_price": cp,
                        "entered_at": pv["entered_at"],
                        "gain_pct": (cp - ep) / ep * 100 if ep > 0 else 0,
                        "peak_gain_pct": (pp - ep) / ep * 100 if ep > 0 else 0,
                        "volume_usdt": pv.get("volume_usdt", 0),
                    })
                send_signal(signal, pool_snapshot if pool_snapshot else None)
            except Exception as e:
                logger.warning(f"信号推送失败: {e}")

    state["watchpool"] = {s: v for s, v in state["watchpool"].items()}
    save_state(state)
    return events


def run():
    """主循环：30秒扫描 + 观察池10秒刷新"""
    logger.info("=== 做多阻击扫描器启动（第一阶段：纯记录） ===")
    state = load_state()
    last_full_scan = 0

    while True:
        now = time.time()

        # 每30秒全市场扫描
        if now - last_full_scan >= CFG["scan_interval_sec"]:
            try:
                tickers = get_all_tickers()
                logger.info(f"全市场扫描: {len(tickers)}个合约")
                events = scan_once(state, tickers)

                if events["new_radar"]:
                    logger.info(f"新雷达: {events['new_radar']}")
                if events["new_pool"]:
                    logger.info(f"新进池: {events['new_pool']}")
                if events["new_signals"]:
                    logger.info(f"新信号: {[s['symbol'] for s in events['new_signals']]}")

                last_full_scan = now
            except Exception as e:
                logger.error(f"扫描异常: {e}")

        # 观察池有币时，每10秒刷新
        elif state["watchpool"]:
            try:
                tickers = get_all_tickers()
                scan_once(state, tickers)
            except Exception as e:
                logger.error(f"观察池刷新异常: {e}")

        # 计算下次唤醒时间
        if state["watchpool"]:
            time.sleep(CFG["watchpool_refresh_sec"])
        else:
            remaining = CFG["scan_interval_sec"] - (time.time() - last_full_scan)
            time.sleep(max(1, remaining))


if __name__ == "__main__":
    run()
