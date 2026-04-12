#!/usr/bin/env python3
"""
bull_sniper scanner.py — 做多阻击扫描器 v3.0

三层过滤架构：
  第一层（全市场30秒扫描）：基础过滤
    24h成交额≥2000万 / 上线≥1天 / 距ATH≥60%（新币<180天豁免） / 2h涨幅<40%
  第二层（候选池→观察池）：动量确认
    1小时涨幅≥5% / OI≥500万U / 量比≥2倍
  第三层（观察池触发）：用1小时涨幅判断
    1h 8-35% AND 5m≥5% → 分析+买入
    1h>35% → 退出（不追高）
    1h<3%  → 退出（动力不足）
    超时30分钟 → 退出
"""
import json
import logging
import time
import yaml
import requests
from datetime import datetime
from pathlib import Path

from analyzer import analyze
from notifier import send_signal, send_health_report, send_status_card, send_trade_report
from buyer import execute as buyer_execute

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

FAPI_BASE = "https://fapi.binance.com"


# ══════════════════════════════════════════
# 币安API
# ══════════════════════════════════════════

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
                "symbol":      t["symbol"],
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


def fetch_market_data(symbol: str) -> dict:
    """拉取打分所需市场数据"""
    return {
        "oi_change_pct":    get_oi_change(symbol),
        "long_short_ratio": get_lsr(symbol),
        "funding_rate":     get_funding_rate(symbol),
        "volume_ratio":     calc_volume_ratio(symbol),
    }


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
    过滤检查
    返回 (True, "") 或 (False, 原因)
    """
    # 2h涨幅过滤（替代24h，更敏感）
    change_2h = calc_2h_change(symbol)
    exclude_2h = CFG.get("exclude_2h_above", 40)
    if change_2h > exclude_2h:
        return False, f"2h已涨{change_2h:.1f}%>{exclude_2h}%"

    # 日成交额不足
    min_vol = CFG["exclude_daily_vol_below"]
    if ticker["volume_usdt"] < min_vol:
        return False, f"成交额{ticker['volume_usdt']/1e6:.1f}M<{min_vol/1e6:.0f}M"

    # 上线天数检查
    ath_data = _load_ath(symbol)
    listing_days = ath_data["listing_days"]
    if listing_days < CFG["min_listing_days"]:
        return False, f"上线{listing_days}天<{CFG['min_listing_days']}天"

    # 距ATH跌幅不足（新币豁免：上线<ath_exempt_days天的币跳过此检查）
    ath_exempt = CFG.get("ath_exempt_days", 180)
    if listing_days >= ath_exempt:
        min_drop = CFG["min_drop_from_ath"]
        if ath_data["drop_from_ath"] < min_drop:
            return False, f"距ATH跌{ath_data['drop_from_ath']:.1f}%<{min_drop}%"

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
        "signals":    [],   # 已触发信号记录
        "cooldowns":  {},   # {symbol: expire_ts}
        "filter_log": [],   # 过滤日志，滚动50条
        "stats": {"scans": 0, "pool_entries": 0, "signals": 0},
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ══════════════════════════════════════════
# 核心扫描逻辑
# ══════════════════════════════════════════

def scan_once(state: dict, tickers: list) -> dict:
    """
    三层过滤扫描
    第一层：基础过滤（24h成交额/上线天数/距ATH/24h涨幅）
    第二层：动量确认（1h涨幅/OI/量比）→ 进观察池
    第三层：观察池触发（1h 8-18% AND 5m≥5%）→ 分析+买入
    """
    now = time.time()
    ticker_map = {t["symbol"]: t for t in tickers}
    events = {"new_pool": [], "new_signals": [], "pool_exits": []}

    state["stats"]["scans"] += 1

    # ── 1. 清理过期冷却 ──
    state["cooldowns"] = {s: ts for s, ts in state["cooldowns"].items() if ts > now}

    # ── 2. 第一层：全市场基础过滤 ──
    candidates = []
    for t in tickers:
        symbol = t["symbol"]

        # 跳过：已在观察池 / 冷却中
        if symbol in state["watchpool"] or symbol in state["cooldowns"]:
            continue

        # 基础过滤
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

    # ── 3. 第二层：动量确认（1h涨幅/OI/量比）→ 进观察池 ──
    pool_1h_threshold = CFG.get("pool_entry_1h_change", 5)
    min_oi = CFG.get("min_oi_usdt", 5000000)
    min_vol_ratio = CFG.get("min_volume_ratio", 2)

    for t in candidates:
        symbol = t["symbol"]

        # 1小时涨幅检查
        change_1h = calc_1h_change(symbol)
        if change_1h < pool_1h_threshold:
            continue

        # OI检查
        oi = get_oi_usdt(symbol)
        if oi < min_oi:
            logger.debug(f"[第二层过滤] {symbol} OI {oi/1e6:.1f}M < {min_oi/1e6:.0f}M")
            state["cooldowns"][symbol] = now + 1800  # 不够格的也冷却30分钟
            continue

        # 量比检查
        vol_ratio = calc_volume_ratio(symbol)
        if vol_ratio < min_vol_ratio:
            logger.debug(f"[第二层过滤] {symbol} 量比{vol_ratio:.1f}x < {min_vol_ratio}x")
            state["cooldowns"][symbol] = now + 1800
            continue

        # 观察池已满，淘汰最弱的（进池后涨幅最低）
        max_pool = CFG.get("watchpool_max", 30)
        if len(state["watchpool"]) >= max_pool:
            weakest = min(state["watchpool"].items(),
                          key=lambda x: x[1].get("gain_since_entry", 0))
            del state["watchpool"][weakest[0]]
            logger.info(f"[观察池] 已满，淘汰最弱 {weakest[0]} ({weakest[1].get('gain_since_entry', 0):.1f}%)")

        # 进入观察池
        ath_data = _load_ath(symbol)
        state["watchpool"][symbol] = {
            "entered_at":    now,
            "entry_price":   t["price"],
            "peak_price":    t["price"],
            "cur_price":     t["price"],
            "change_1h":     change_1h,
            "oi_usdt":       oi,
            "vol_ratio":     vol_ratio,
            "volume_usdt":   t["volume_usdt"],
            "drop_from_ath": ath_data["drop_from_ath"],
            "analyzed":      False,
            "last_analyze_price": 0,
            "last_analyze_time":  0,
        }
        state["stats"]["pool_entries"] += 1
        events["new_pool"].append(symbol)
        logger.info(
            f"[进池] {symbol} 1h+{change_1h:.1f}% OI:{oi/1e6:.1f}M "
            f"量比:{vol_ratio:.1f}x 价格:{t['price']} "
            f"24h量:{t['volume_usdt']/1e6:.1f}M 距ATH跌{ath_data['drop_from_ath']:.1f}%"
        )

    # ── 4. 第三层：观察池管理（零成本观察，定期打分） ──
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

        # ── 退出条件：池满淘汰最弱（无超时、无涨跌幅退出） ──
        # 池内零成本观察，不主动退出，由池满淘汰机制处理

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
        market_data = fetch_market_data(symbol)
        market_data["change_1m"] = change_1m
        market_data["change_3m"] = change_3m
        market_data["change_5m"] = change_5m

        logger.info(f"[分析] {symbol} 池内+{gain_since_entry:.1f}% 1h+{change_1h:.1f}% 5m+{change_5m:.1f}% 1m+{change_1m:.1f}%")
        analyze_result = analyze(symbol, change_1h, market_data, cfg=CFG)
        if analyze_result:
            analyze_result["market_data"] = market_data

        if analyze_result is None:
            continue

        logger.info(
            f"[分析结果] {symbol}: {analyze_result['action']} "
            f"{analyze_result.get('reason', '')} "
            f"score={analyze_result.get('score', '-')}"
        )

        # ── 信号触发 ──
        if analyze_result["action"] in ("signal_fast", "signal_scored"):
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
            state["cooldowns"][symbol] = now + CFG.get("cooldown_after_tp_hours", 4) * 3600
            del state["watchpool"][symbol]
            events["new_signals"].append(signal)

            logger.info(
                f"[信号] {symbol} 1h+{change_1h:.1f}% "
                f"原因:{signal['reason']} score={signal['score']}"
            )

            # 推送通知
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
                    # 推送成交详情报告给乌鸦
                    try:
                        send_trade_report(signal, buy_result, analyze_result)
                    except Exception as e2:
                        logger.warning(f"成交报告推送失败 {symbol}: {e2}")
                except Exception as e:
                    logger.error(f"买入执行失败 {symbol}: {e}")

        # ── 利空否决 ──
        elif analyze_result["action"] == "veto":
            del state["watchpool"][symbol]
            state["cooldowns"][symbol] = now + 1800
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

    save_state(state)
    return events


# ══════════════════════════════════════════
# 主循环
# ══════════════════════════════════════════

def run():
    logger.info("=== 做多阻击扫描器 v3.0 启动 ===")
    state = load_state()
    last_full_scan    = 0
    last_health_report = 0

    while True:
        now = time.time()

        # 每小时整点：群组状态卡片 + 私信健康报告（60秒窗口防漏）
        if int(now) % 3600 < 60 and now - last_health_report > 300:
            try:
                send_status_card(state)
                logger.info("[状态卡片] 已推群组")
            except Exception as e:
                logger.warning(f"状态卡片推送失败: {e}")
            try:
                send_health_report(state, state.get("filter_log", []))
                state["stats"] = {"scans": 0, "pool_entries": 0, "signals": 0}
                last_health_report = now
                logger.info("[健康报告] 已推送")
            except Exception as e:
                logger.warning(f"健康报告推送失败: {e}")

        # 每30秒全市场扫描
        if now - last_full_scan >= CFG.get("scan_interval_sec", 30):
            try:
                tickers = get_all_tickers()
                logger.info(f"全市场扫描: {len(tickers)}个合约")
                events = scan_once(state, tickers)

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

        # 睡眠
        if state["watchpool"]:
            time.sleep(CFG.get("watchpool_refresh_sec", 10))
        else:
            remaining = CFG.get("scan_interval_sec", 30) - (time.time() - last_full_scan)
            time.sleep(max(1, remaining))


if __name__ == "__main__":
    run()
