"""
chain_score.py — E因子：捉妖评分模块 v1.0
数据源：币安 Web3 Skills API（免费无认证）

反向逻辑：庄控越重=弹药越足=拉盘越容易=加分
聪明钱流出=庄家在跑=一票否决

E1 弹药确认（holdersTop10Percent）: 0~+5
E2 庄家动向（inflow+traders）: -3~+5 / 否决
E3 造势力度（holders+增长+KYC+热度）: -3~+12
E4 买卖节奏（countBuy/countSell）: -2~+3
E5 合约安全（honeypot/riskLevel）: 否决

2026-04-14 v1.0 按顾问方案部署
"""
import time
import logging
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

ALPHA_LIST_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"

_alpha_cache = {"ts": 0, "data": {}}
ALPHA_TTL = 600

_holders_snapshot: dict = {}
SNAPSHOT_FILE = Path(__file__).parent / "data" / "holders_snapshot.json"


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


_load_holders_snapshot()


def _get_alpha_map() -> dict:
    now = time.time()
    if now - _alpha_cache["ts"] < ALPHA_TTL and _alpha_cache["data"]:
        return _alpha_cache["data"]
    try:
        resp = requests.get(ALPHA_LIST_URL, headers=H_GET, timeout=10)
        data = resp.json().get("data", [])
        if isinstance(data, dict):
            data = data.get("list", [])
        mapping = {}
        for t in data:
            sym = (t.get("symbol") or "").upper()
            ca = t.get("contractAddress", "")
            if sym and ca:
                mapping[sym] = ca
        _alpha_cache["ts"] = now
        _alpha_cache["data"] = mapping
        return mapping
    except Exception as e:
        logger.warning(f"[E因子] Alpha列表获取失败: {e}")
        return _alpha_cache.get("data", {})


def _get_inflow_data(contract_address: str) -> dict:
    for period in ["1h", "4h"]:
        try:
            resp = requests.post(
                f"{BASE}/tracker/wallet/token/inflow/rank/query/ai",
                headers=H_POST,
                json={"chainId": "56", "period": period, "tagType": 2},
                timeout=10,
            )
            data = resp.json().get("data", [])
            for item in data:
                if (item.get("ca") or "").lower() == contract_address.lower():
                    item["_period"] = period
                    return item
        except Exception as e:
            logger.debug(f"[E因子] inflow {period} 异常: {e}")
    return {}


def _get_social_data(contract_address: str) -> dict:
    for chain in ["56", "CT_501"]:
        try:
            url = (
                f"{BASE}/buw/wallet/market/token/pulse/social/hype/"
                f"rank/leaderboard/ai?chainId={chain}"
                f"&sentiment=All&socialLanguage=ALL&targetLanguage=en&timeRange=1"
            )
            resp = requests.get(url, headers=H_GET, timeout=10)
            body = resp.json().get("data", {})
            lst = body.get("leaderBoardList", body.get("list", [])) if isinstance(body, dict) else []
            for item in lst:
                meta = item.get("metaInfo") or {}
                ca = (meta.get("contractAddress") or "").lower()
                if ca == contract_address.lower():
                    hi = item.get("socialHypeInfo") or {}
                    return {
                        "sentiment": hi.get("sentiment", ""),
                        "hype": float(hi.get("socialHype", 0)),
                    }
        except Exception as e:
            logger.debug(f"[E因子] social {chain} 异常: {e}")
    return {}


def get_chain_score(symbol: str, cfg: dict = None) -> dict:
    """
    E因子主入口 — 捉妖评分
    返回: {"score": int, "reason": str, "vetoed": bool, "veto_reason": str, "detail": dict}
    """
    ticker = symbol.replace("USDT", "").replace("BUSD", "").upper()
    detail = {}

    alpha_map = _get_alpha_map()
    ca = alpha_map.get(ticker, "")
    if not ca:
        logger.info(f"[E因子] {symbol} Alpha列表无合约地址 → 返回0分")
        return {"score": 0, "reason": "非Alpha币", "vetoed": False, "veto_reason": "", "detail": {}}

    ca_short = f"{ca[:10]}...{ca[-4:]}"
    log_lines = [f"[E因子] {symbol} | 合约:{ca_short}"]

    inflow = _get_inflow_data(ca)
    social = _get_social_data(ca)

    score = 0
    reasons = []
    vetoed = False
    veto_reason = ""

    # ── E5 合约安全（先判断，否决优先）──
    if inflow:
        risk_codes = inflow.get("tokenRiskCodes") or []
        risk_level = inflow.get("tokenRiskLevel", 0)
        if isinstance(risk_level, str):
            risk_level = int(risk_level) if risk_level.isdigit() else 0

        if "honeypot" in risk_codes:
            vetoed = True
            veto_reason = "蜜罐合约"
        elif risk_level == 3:
            vetoed = True
            veto_reason = f"高危合约 level={risk_level}"

        codes_str = ",".join(risk_codes) if risk_codes else "无"
        log_lines.append(f"E5:level={risk_level},codes=[{codes_str}]{'→否决' if vetoed else '→通过'}")
        detail["E5_risk_level"] = risk_level
        detail["E5_risk_codes"] = risk_codes

    if vetoed:
        logger.info(" | ".join(log_lines))
        return {"score": 0, "reason": veto_reason, "vetoed": True, "veto_reason": veto_reason, "detail": detail}

    # ── E1 弹药确认 ──
    top10 = float(inflow.get("holdersTop10Percent", 0)) if inflow else 0
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

    # ── E2 庄家动向 ──
    if inflow:
        inf_val = float(inflow.get("inflow", 0))
        traders = int(inflow.get("traders", 0))
        period_used = inflow.get("_period", "?")

        if inf_val < 0 and traders >= 2:
            vetoed = True
            veto_reason = f"聪明钱流出${inf_val:,.0f} {traders}地址"
            e2 = 0
        elif inf_val < 0 and traders == 1:
            e2 = -3
        elif inf_val > 0 and traders >= 3:
            e2 = 5
        elif inf_val > 0 and traders >= 1:
            e2 = 3
        else:
            e2 = 0

        score += e2
        detail["E2_inflow"] = round(inf_val, 2)
        detail["E2_traders"] = traders
        detail["E2_score"] = e2
        tag = "→否决" if vetoed else f"={e2:+d}"
        log_lines.append(f"E2:inflow={inf_val:,.0f},traders={traders}({period_used}){tag}")
        if e2 > 0:
            reasons.append(f"聪明钱+{e2}")
        elif e2 < 0:
            reasons.append(f"聪明钱{e2}")

    if vetoed:
        logger.info(" | ".join(log_lines))
        return {"score": score, "reason": veto_reason, "vetoed": True, "veto_reason": veto_reason, "detail": detail}

    # ── E3a 假地址力度 ──
    holders = int(inflow.get("holders", 0)) if inflow else 0
    if holders >= 50000:
        e3a = 5
    elif holders >= 30000:
        e3a = 4
    elif holders >= 20000:
        e3a = 3
    else:
        e3a = 0
    score += e3a
    detail["E3a_holders"] = holders
    detail["E3a_score"] = e3a
    log_lines.append(f"E3a:{holders}={e3a:+d}")
    if e3a > 0:
        reasons.append(f"持有{holders}+{e3a}")

    # ── E3b 5分钟增长 ──
    e3b = 0
    if ca in _holders_snapshot and holders > 0:
        prev = _holders_snapshot[ca].get("holders", 0)
        if holders > prev:
            e3b = 3
        log_lines.append(f"E3b:{prev}→{holders}={e3b:+d}")
    else:
        log_lines.append(f"E3b:首次=0")
    if holders > 0:
        _holders_snapshot[ca] = {"holders": holders, "ts": time.time()}
        _save_holders_snapshot()
    score += e3b
    detail["E3b_score"] = e3b

    # ── E3c KYC占比 ──
    kyc = int(inflow.get("kycHolders") or 0) if inflow else 0
    kyc_pct = (kyc / holders * 100) if holders > 0 else 100
    if kyc_pct < 5:
        e3c = 2
    elif kyc_pct <= 20:
        e3c = 1
    else:
        e3c = 0
    score += e3c
    detail["E3c_kyc_pct"] = round(kyc_pct, 1)
    detail["E3c_score"] = e3c
    log_lines.append(f"E3c:KYC{kyc_pct:.1f}%={e3c:+d}")

    # ── E3d 社交热度 ──
    hype = social.get("hype", 0)
    sentiment = social.get("sentiment", "")
    if hype > 1_000_000 and sentiment == "Negative":
        e3d = -3
    elif hype < 100_000:
        e3d = 2
    elif hype < 500_000:
        e3d = 1
    else:
        e3d = 0
    score += e3d
    detail["E3d_hype"] = hype
    detail["E3d_sentiment"] = sentiment
    detail["E3d_score"] = e3d
    hype_str = f"{hype/10000:.0f}万" if hype >= 10000 else str(int(hype))
    log_lines.append(f"E3d:{hype_str}/{sentiment or '无'}={e3d:+d}")
    if e3d != 0:
        reasons.append(f"热度{e3d:+d}")

    # ── E4 买卖节奏 ──
    buy_cnt = int(inflow.get("countBuy", 0)) if inflow else 0
    sell_cnt = int(inflow.get("countSell", 0)) if inflow else 0
    ratio = buy_cnt / sell_cnt if sell_cnt > 0 else 1.0
    if ratio > 1.5:
        e4 = 3
    elif ratio > 1.2:
        e4 = 2
    elif ratio >= 0.9:
        e4 = 0
    else:
        e4 = -2
    score += e4
    detail["E4_buy_sell_ratio"] = round(ratio, 2)
    detail["E4_score"] = e4
    log_lines.append(f"E4:{ratio:.2f}={e4:+d}")
    if e4 != 0:
        reasons.append(f"买卖{e4:+d}")

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
