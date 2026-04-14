"""
chain_score.py — E因子：捉妖评分模块 v2.0
数据源：币安 Web3 Skills API query-token-info（免费无认证）

反向逻辑：庄控越重=弹药越足=拉盘越容易=加分
聪明钱流出=庄家在跑=一票否决

E1  弹药确认（top10HoldersPercentage）: 0~+5
E2a 聪明钱持仓（smartMoneyHolders）: 0~+3
E2b 币安净流向（volume24hNetBinance）: -2~+2
E2c 聪明钱流出否决（inflow端点交叉验证）: 否决
E3a 新钱包占比（newWalletHolders/holders）: 0~+5
E3b 批量地址（bundlerHolders）: 0~+3
E3c holders门槛: 0~+5
E3d 5分钟增长（本地快照）: 0~+3
E4a 1h买卖比: -2~+3
E4b 5m加速确认: 0~+2

2026-04-14 v2.0 按顾问v2方案部署
"""
import time
import logging
import os
import requests
from pathlib import Path
import json

logger = logging.getLogger("bull_sniper.chain_score")

BASE = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct"
H_GET = {"Accept-Encoding": "identity", "User-Agent": "binance-web3/2.1 (Skill)"}
H_POST = {
    "Content-Type": "application/json",
    "Accept-Encoding": "identity",
    "User-Agent": "binance-web3/2.1 (Skill)",
}

SEARCH_URL = "https://web3.binance.com/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search/ai"
DYNAMIC_URL = "https://web3.binance.com/bapi/defi/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info/ai"

API_TIMEOUT = 5

# ── 合约地址缓存（内存 + 磁盘） ──
ALPHA_CACHE_FILE = Path(__file__).parent / "data" / "alpha_cache.json"
_ca_cache: dict = {"ts": 0, "data": {}}
CA_CACHE_TTL = 3600

# ── holders快照 ──
_holders_snapshot: dict = {}
SNAPSHOT_FILE = Path(__file__).parent / "data" / "holders_snapshot.json"

# ── API失败计数 + 告警 ──
_fail_count = 0
_fail_alerted = False
FAIL_ALERT_THRESHOLD = 3

BOT_TOKEN = os.getenv("PUSH_BOT_TOKEN", "") or os.getenv("BOT_TOKEN", "")
ADMIN_ID = os.getenv("ADMIN_ID", "509640925")


def _tg_alert(text: str):
    if not BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_ID, "text": text},
            timeout=5,
        )
    except Exception:
        pass


def _load_holders_snapshot():
    global _holders_snapshot
    if SNAPSHOT_FILE.exists():
        try:
            _holders_snapshot = json.loads(SNAPSHOT_FILE.read_text())
        except Exception:
            _holders_snapshot = {}


def _save_holders_snapshot():
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    keep = {}
    now = time.time()
    for k, v in _holders_snapshot.items():
        if now - v.get("ts", 0) < 86400 * 3:
            keep[k] = v
    SNAPSHOT_FILE.write_text(json.dumps(keep, ensure_ascii=False))


def _load_alpha_cache():
    global _ca_cache
    if ALPHA_CACHE_FILE.exists():
        try:
            disk = json.loads(ALPHA_CACHE_FILE.read_text())
            _ca_cache["data"] = {k: v.get("address", v) if isinstance(v, dict) else v for k, v in disk.items()}
            _ca_cache["ts"] = ALPHA_CACHE_FILE.stat().st_mtime
        except Exception:
            pass


def _save_alpha_cache():
    ALPHA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    out = {}
    for ticker, addr in _ca_cache["data"].items():
        out[ticker] = {"address": addr, "chainId": "56", "updated": now}
    ALPHA_CACHE_FILE.write_text(json.dumps(out, ensure_ascii=False))


_load_holders_snapshot()
_load_alpha_cache()


def _api_ok():
    global _fail_count, _fail_alerted
    if _fail_alerted:
        _tg_alert("[E因子] API恢复正常，退出降级模式")
        logger.info("[E因子v2] API恢复")
    _fail_count = 0
    _fail_alerted = False


def _api_fail(msg: str):
    global _fail_count, _fail_alerted
    _fail_count += 1
    logger.warning(f"[E因子v2] API失败({_fail_count}): {msg}")
    if _fail_count >= FAIL_ALERT_THRESHOLD and not _fail_alerted:
        _fail_alerted = True
        _tg_alert(f"[E因子告警] API连续{_fail_count}次失败，已降级为纯BCD评分模式")


def _search_contract(ticker: str) -> str:
    now = time.time()
    if ticker in _ca_cache["data"] and now - _ca_cache["ts"] < CA_CACHE_TTL:
        return _ca_cache["data"][ticker]

    for attempt in range(2):
        try:
            resp = requests.get(SEARCH_URL, params={"keyword": ticker}, headers=H_GET, timeout=API_TIMEOUT)
            items = resp.json().get("data", [])
            for item in items:
                sym = (item.get("symbol") or "").upper()
                chain = item.get("chainId", "")
                ca = item.get("contractAddress", "")
                if sym == ticker and chain == "56" and ca:
                    _ca_cache["data"][ticker] = ca
                    _ca_cache["ts"] = now
                    _save_alpha_cache()
                    return ca
            for item in items:
                sym = (item.get("symbol") or "").upper()
                ca = item.get("contractAddress", "")
                if sym == ticker and ca:
                    _ca_cache["data"][ticker] = ca
                    _ca_cache["ts"] = now
                    _save_alpha_cache()
                    return ca
            break
        except Exception as e:
            if attempt == 0:
                continue
            logger.warning(f"[E因子v2] search重试失败: {e}")

    if ticker in _ca_cache["data"]:
        logger.info(f"[E因子v2] search失败，走本地缓存 {ticker}")
        return _ca_cache["data"][ticker]
    return ""


def _get_dynamic_data(contract_address: str) -> dict:
    for attempt in range(2):
        try:
            resp = requests.get(
                DYNAMIC_URL,
                params={"chainId": "56", "contractAddress": contract_address},
                headers=H_GET,
                timeout=API_TIMEOUT,
            )
            data = resp.json().get("data") or {}
            if data:
                _api_ok()
                return data
        except Exception as e:
            if attempt == 0:
                continue
            _api_fail(f"dynamic {e}")
    return {}


def _get_inflow_data(contract_address: str) -> dict:
    for period in ["1h", "4h"]:
        try:
            resp = requests.post(
                f"{BASE}/tracker/wallet/token/inflow/rank/query/ai",
                headers=H_POST,
                json={"chainId": "56", "period": period, "tagType": 2},
                timeout=API_TIMEOUT,
            )
            data = resp.json().get("data", [])
            for item in data:
                if (item.get("ca") or "").lower() == contract_address.lower():
                    item["_period"] = period
                    return item
        except Exception as e:
            logger.debug(f"[E因子v2] inflow {period} 异常: {e}")
    return {}


def get_chain_score(symbol: str, cfg: dict = None) -> dict:
    """
    E因子v2主入口 — 捉妖评分
    返回: {"score": int, "reason": str, "vetoed": bool, "veto_reason": str, "detail": dict}
    """
    ticker = symbol.replace("USDT", "").replace("BUSD", "").upper()
    detail = {}
    zero = {"score": 0, "reason": "", "vetoed": False, "veto_reason": "", "detail": {}}

    ca = _search_contract(ticker)
    if not ca:
        logger.info(f"[E因子v2] {symbol} 搜索无合约地址 → 返回0分")
        zero["reason"] = "非Alpha币"
        return zero

    ca_short = f"{ca[:10]}...{ca[-4:]}"
    log_lines = [f"[E因子v2] {symbol} | 合约:{ca_short}"]

    dyn = _get_dynamic_data(ca)
    if not dyn:
        logger.info(f"[E因子v2] {symbol} dynamic为空(降级) → 返回0分")
        zero["reason"] = "API降级"
        return zero

    inflow = _get_inflow_data(ca)

    score = 0
    reasons = []
    vetoed = False
    veto_reason = ""

    # ── E1 弹药确认 ──
    top10 = float(dyn.get("top10HoldersPercentage", 0))
    if top10 >= 97:
        e1 = 5
    elif top10 >= 93:
        e1 = 4
    elif top10 >= 85:
        e1 = 2
    else:
        e1 = 0
    score += e1
    detail["E1_top10pct"] = round(top10, 2)
    detail["E1_score"] = e1
    log_lines.append(f"E1:{top10:.1f}%={e1:+d}")
    if e1 > 0:
        reasons.append(f"集中{top10:.0f}%+{e1}")

    # ── E2a 聪明钱持仓 ──
    sm_holders = int(dyn.get("smartMoneyHolders", 0))
    sm_pct = float(dyn.get("smartMoneyHoldingPercent", 0))
    if sm_holders >= 5 and sm_pct > 0.1:
        e2a = 3
    elif sm_holders >= 1 and sm_pct <= 0.1:
        e2a = 1
    else:
        e2a = 0
    score += e2a
    detail["E2a_sm_holders"] = sm_holders
    detail["E2a_sm_pct"] = round(sm_pct, 4)
    detail["E2a_score"] = e2a
    log_lines.append(f"E2a:{sm_holders}个/{sm_pct:.3f}%={e2a:+d}")
    if e2a > 0:
        reasons.append(f"聪明钱持仓+{e2a}")

    # ── E2b 币安净流向 ──
    bn_net = float(dyn.get("volume24hNetBinance", 0))
    if bn_net < -100000:
        e2b = 2
    elif bn_net < 0:
        e2b = 1
    elif bn_net <= 100000:
        e2b = 0
    else:
        e2b = -2
    score += e2b
    detail["E2b_bn_net"] = round(bn_net, 2)
    detail["E2b_score"] = e2b
    bn_net_str = f"${bn_net/1000:,.0f}k"
    log_lines.append(f"E2b:币安净流{bn_net_str}={e2b:+d}")
    if e2b != 0:
        reasons.append(f"币安流向{e2b:+d}")

    # ── E2c 聪明钱流出否决（inflow端点交叉验证） ──
    if inflow:
        inf_val = float(inflow.get("inflow", 0))
        traders = int(inflow.get("traders", 0))
        period_used = inflow.get("_period", "?")
        if inf_val < 0 and traders >= 2:
            vetoed = True
            veto_reason = f"聪明钱流出${inf_val:,.0f} {traders}地址"
        detail["E2c_inflow"] = round(inf_val, 2)
        detail["E2c_traders"] = traders
        tag = "→否决" if vetoed else "通过"
        log_lines.append(f"E2c:inflow={inf_val:,.0f},traders={traders}({period_used}){tag}")
    else:
        log_lines.append(f"E2c:inflow端点无数据=跳过")

    if vetoed:
        detail["E_total"] = score
        logger.info(" | ".join(log_lines))
        return {"score": score, "reason": veto_reason, "vetoed": True, "veto_reason": veto_reason, "detail": detail}

    # ── E3a 新钱包占比 ──
    holders = int(dyn.get("holders", 0))
    new_wallet = int(dyn.get("newWalletHolders", 0))
    new_pct = (new_wallet / holders * 100) if holders > 0 else 0
    if new_pct > 60:
        e3a = 5
    elif new_pct > 40:
        e3a = 4
    elif new_pct > 20:
        e3a = 2
    else:
        e3a = 0
    score += e3a
    detail["E3a_new_wallet_pct"] = round(new_pct, 1)
    detail["E3a_score"] = e3a
    log_lines.append(f"E3a:新钱包{new_pct:.1f}%={e3a:+d}")
    if e3a > 0:
        reasons.append(f"新钱包{new_pct:.0f}%+{e3a}")

    # ── E3b 批量打包地址 ──
    bundler = int(dyn.get("bundlerHolders", 0))
    if bundler > 2000:
        e3b = 3
    elif bundler > 1000:
        e3b = 2
    elif bundler > 500:
        e3b = 1
    else:
        e3b = 0
    score += e3b
    detail["E3b_bundler"] = bundler
    detail["E3b_score"] = e3b
    log_lines.append(f"E3b:bundler={bundler}={e3b:+d}")
    if e3b > 0:
        reasons.append(f"bundler{bundler}+{e3b}")

    # ── E3c holders门槛 ──
    if holders >= 50000:
        e3c = 5
    elif holders >= 30000:
        e3c = 4
    elif holders >= 20000:
        e3c = 3
    else:
        e3c = 0
    score += e3c
    detail["E3c_holders"] = holders
    detail["E3c_score"] = e3c
    log_lines.append(f"E3c:holders={holders}={e3c:+d}")
    if e3c > 0:
        reasons.append(f"持有{holders}+{e3c}")

    # ── E3d 5分钟增长 ──
    e3d = 0
    if ca in _holders_snapshot and holders > 0:
        prev = _holders_snapshot[ca].get("holders", 0)
        if holders > prev:
            e3d = 3
        log_lines.append(f"E3d:{prev}→{holders}={e3d:+d}")
    else:
        log_lines.append(f"E3d:首次=0")
    if holders > 0:
        _holders_snapshot[ca] = {"holders": holders, "ts": time.time()}
        _save_holders_snapshot()
    score += e3d
    detail["E3d_score"] = e3d

    # ── E4a 1h买卖比 ──
    vol_1h_buy = float(dyn.get("volume1hBuy", 0))
    vol_1h_sell = float(dyn.get("volume1hSell", 0))
    ratio_1h = vol_1h_buy / vol_1h_sell if vol_1h_sell > 0 else 1.0
    if ratio_1h > 1.5:
        e4a = 3
    elif ratio_1h > 1.2:
        e4a = 2
    elif ratio_1h >= 0.9:
        e4a = 0
    else:
        e4a = -2
    score += e4a
    detail["E4a_ratio_1h"] = round(ratio_1h, 2)
    detail["E4a_score"] = e4a
    log_lines.append(f"E4a:1h买卖{ratio_1h:.2f}={e4a:+d}")
    if e4a != 0:
        reasons.append(f"1h买卖{e4a:+d}")

    # ── E4b 5m加速确认 ──
    vol_5m_buy = float(dyn.get("volume5mBuy", 0))
    vol_5m_sell = float(dyn.get("volume5mSell", 0))
    ratio_5m = vol_5m_buy / vol_5m_sell if vol_5m_sell > 0 else 1.0
    if ratio_5m > 2.0:
        e4b = 2
    else:
        e4b = 0
    score += e4b
    detail["E4b_ratio_5m"] = round(ratio_5m, 2)
    detail["E4b_score"] = e4b
    log_lines.append(f"E4b:5m买卖{ratio_5m:.2f}={e4b:+d}")
    if e4b > 0:
        reasons.append(f"5m加速+{e4b}")

    reason = " | ".join(reasons) if reasons else "E因子无加分"
    detail["E_total"] = score
    log_lines.append(f"总分={score:+d}")
    logger.info(" | ".join(log_lines))

    return {
        "score": score,
        "reason": reason,
        "vetoed": vetoed,
        "veto_reason": veto_reason,
        "detail": detail,
    }
