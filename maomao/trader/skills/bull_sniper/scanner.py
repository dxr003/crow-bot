#!/usr/bin/env python3
"""
bull_sniper scanner.py — 做多阻击扫描器 v2.0

触发机制（单层，干净）：
  每30秒扫全市场 → 24h涨幅≥8%且通过过滤 → 进入观察池
  观察池每10秒刷新：
    涨幅8-10%  → analyzer第一阶段（新闻+下架公告）→ 有利好直接推
    涨幅10-20% → analyzer第二阶段（综合打分+AI）→ ≥30分推信号
    涨幅>20%   → 退出（不追高）
    涨幅<5%    → 退出（动力不足）
    超时30分钟  → 退出

过滤条件：
  - 上线<30天排除
  - 24h涨幅已>30%排除（已涨过头）
  - 日成交额<500万U排除
  - 距历史ATH跌幅<50%排除（未腰斩不碰）
"""
import json
import logging
import time
import yaml
import requests
from datetime import datetime
from pathlib import Path

from analyzer import analyze
from notifier import send_signal, send_health_report, send_status_card
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


def get_oi_change(symbol: str) -> float:
    """获取OI变化（占位，币安无历史OI接口，后续可接Coinglass）"""
    return 0.0


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
    # 24h涨幅已>30%（已涨过头）
    if ticker["change_pct"] > CFG["exclude_24h_above"]:
        return False, f"24h已涨{ticker['change_pct']:.1f}%>30%"

    # 日成交额<500万U
    if ticker["volume_usdt"] < CFG["exclude_daily_vol_below"]:
        return False, f"成交额{ticker['volume_usdt']/1e6:.1f}M<5M"

    # 上线天数<30
    ath_data = _load_ath(symbol)
    if ath_data["listing_days"] < CFG["min_listing_days"]:
        return False, f"上线{ath_data['listing_days']}天<30天"

    # 距ATH跌幅<50%（未腰斩）
    if ath_data["drop_from_ath"] < CFG["min_drop_from_ath"]:
        return False, f"距ATH跌{ath_data['drop_from_ath']:.1f}%<50%"

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
    执行一次完整扫描
    返回本次产生的事件：new_pool / new_signals / pool_exits
    """
    now = time.time()
    ticker_map = {t["symbol"]: t for t in tickers}
    events = {"new_pool": [], "new_signals": [], "pool_exits": []}

    state["stats"]["scans"] += 1

    # ── 1. 清理过期冷却 ──
    state["cooldowns"] = {s: ts for s, ts in state["cooldowns"].items() if ts > now}

    # ── 2. 全市场扫描：24h涨幅≥8%进观察池 ──
    pool_entry_threshold = CFG.get("pool_entry_gain_pct", 8)

    for t in tickers:
        symbol = t["symbol"]

        # 跳过：已在观察池 / 冷却中
        if symbol in state["watchpool"] or symbol in state["cooldowns"]:
            continue

        # 24h涨幅未达进池门槛
        if t["change_pct"] < pool_entry_threshold:
            continue

        # 过滤检查
        ok, reason = passes_filter(symbol, t)
        if not ok:
            # 记录过滤日志（滚动50条）
            state.setdefault("filter_log", []).append({
                "symbol": symbol, "reason": reason,
                "time": datetime.now().strftime("%H:%M:%S"),
            })
            if len(state["filter_log"]) > 50:
                state["filter_log"] = state["filter_log"][-50:]
            logger.debug(f"[过滤] {symbol} {reason}")
            continue

        # 观察池已满，淘汰最早进入的
        max_pool = CFG.get("watchpool_max", 10)
        if len(state["watchpool"]) >= max_pool:
            oldest = min(state["watchpool"].items(), key=lambda x: x[1]["entered_at"])
            del state["watchpool"][oldest[0]]
            logger.info(f"[观察池] 已满，淘汰 {oldest[0]}")

        # 进入观察池
        ath_data = _load_ath(symbol)
        state["watchpool"][symbol] = {
            "entered_at":   now,
            "entry_price":  t["price"],
            "peak_price":   t["price"],
            "cur_price":    t["price"],
            "change_pct":   t["change_pct"],
            "volume_usdt":  t["volume_usdt"],
            "drop_from_ath": ath_data["drop_from_ath"],
            "analyzed":     False,
            "last_analyze_price": 0,
        }
        state["stats"]["pool_entries"] += 1
        events["new_pool"].append(symbol)
        logger.info(
            f"[进池] {symbol} 24h+{t['change_pct']:.1f}% "
            f"价格:{t['price']} 量:{t['volume_usdt']/1e6:.1f}M "
            f"距ATH跌{ath_data['drop_from_ath']:.1f}%"
        )

    # ── 3. 观察池管理 ──
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

        # 从进池价算涨幅
        gain_pct = (cur_price - entry_price) / entry_price * 100 if entry_price > 0 else 0

        # ── 退出条件 ──
        exit_reason = None
        stage2_max = CFG.get("analyzer", {}).get("stage2_gain_max", 20)

        if gain_pct < 5:
            exit_reason = "涨幅回落<5%"
        elif gain_pct > stage2_max:
            exit_reason = f"超过{stage2_max}%不追高"
        elif elapsed_min > CFG.get("watchpool_timeout_min", 30):
            exit_reason = "观察超时30分钟"

        if exit_reason:
            del state["watchpool"][symbol]
            events["pool_exits"].append({"symbol": symbol, "reason": exit_reason})
            logger.info(f"[退出] {symbol} {exit_reason}")
            continue

        # ── 价格变化≥1%重置分析标记 ──
        last_price = pool.get("last_analyze_price", 0)
        if last_price > 0 and abs(cur_price - last_price) / last_price * 100 >= 1:
            pool["analyzed"] = False

        # 已分析过跳过
        if pool.get("analyzed"):
            continue

        # ── 分析触发 ──
        analyzer_cfg  = CFG.get("analyzer", {})
        stage1_min    = analyzer_cfg.get("stage1_gain_pct", 8)
        stage2_min    = analyzer_cfg.get("stage2_gain_min", 10)

        analyze_result = None

        if gain_pct >= stage2_min:
            # 第二阶段：综合打分+AI决策
            logger.info(f"[分析] {symbol} +{gain_pct:.1f}% 进入第二阶段打分")
            market_data    = fetch_market_data(symbol)
            analyze_result = analyze(symbol, gain_pct, market_data, cfg=CFG)

        elif gain_pct >= stage1_min:
            # 第一阶段：新闻+下架快速通道
            logger.info(f"[分析] {symbol} +{gain_pct:.1f}% 进入第一阶段快速通道")
            analyze_result = analyze(symbol, gain_pct, {}, cfg=CFG)

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
                "gain_pct":     round(gain_pct, 1),
                "volume_usdt":  t["volume_usdt"],
                "drop_from_ath": pool.get("drop_from_ath", 0),
                "elapsed_min":  round(elapsed_min, 1),
                "action":       analyze_result["action"],
                "reason":       analyze_result.get("reason", ""),
                "score":        analyze_result.get("score"),
                "ai_reason":    analyze_result.get("ai_reason", ""),
            }
            state["signals"].append(signal)
            state["stats"]["signals"] += 1
            state["cooldowns"][symbol] = now + CFG.get("cooldown_after_tp_hours", 4) * 3600
            del state["watchpool"][symbol]
            events["new_signals"].append(signal)

            logger.info(
                f"[信号] {symbol} +{gain_pct:.1f}% "
                f"原因:{signal['reason']} score={signal['score']}"
            )

            # 推送通知
            try:
                send_signal(signal)
            except Exception as e:
                logger.warning(f"信号推送失败 {symbol}: {e}")

            # 执行买入
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
            except Exception as e:
                logger.error(f"买入执行失败 {symbol}: {e}")

        # ── 利空否决 ──
        elif analyze_result["action"] == "veto":
            del state["watchpool"][symbol]
            events["pool_exits"].append({
                "symbol": symbol,
                "reason": f"否决: {analyze_result.get('reason', '')}",
            })
            logger.info(f"[否决] {symbol} {analyze_result.get('reason', '')}")

        # ── 继续观察 ──
        elif analyze_result["action"] == "hold":
            pool["analyzed"] = True
            pool["last_analyze_price"] = cur_price

    save_state(state)
    return events


# ══════════════════════════════════════════
# 主循环
# ══════════════════════════════════════════

def run():
    logger.info("=== 做多阻击扫描器 v2.0 启动 ===")
    state = load_state()
    last_full_scan    = 0
    last_health_report = 0

    while True:
        now = time.time()

        # 每小时整点：群组状态卡片 + 私信健康报告
        if int(now) % 3600 < CFG.get("watchpool_refresh_sec", 10) and \
                now - last_health_report > 300:
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
