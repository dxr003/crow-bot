# 交易指令解析器 — 关键词 → 标准JSON
import re

# ── 平台映射 ──────────────────────────────────────────
PLATFORM_MAP = {
    "bn": "binance", "binance": "binance", "币安": "binance",
    "hl": "hl", "hyperliquid": "hl",
}

# ── 动作映射 ──────────────────────────────────────────
ACTION_MAP = {
    "开多": "open_long", "做多": "open_long", "多": "open_long", "long": "open_long",
    "开空": "open_short", "做空": "open_short", "空": "open_short", "short": "open_short",
    "加": "add", "加仓": "add", "补仓": "add",
    "平": "close", "平仓": "close", "全平": "close", "清仓": "close",
    "滚": "roll", "滚仓": "roll",
    "移": "trailing", "移动止盈": "trailing",
    "买": "buy", "买入": "buy", "buy": "buy",
    "卖": "sell", "卖出": "sell", "sell": "sell",
    "划转": "transfer", "transfer": "transfer",
    "止盈": "tp", "tp": "tp",
    "止损": "sl", "sl": "sl",
}

# ── 仓位模式映射（默认全仓）──────────────────────────
MARGIN_MAP = {
    "全": "cross", "全仓": "cross", "cross": "cross",
    "zc": "isolated", "逐仓": "isolated", "isolated": "isolated",
}

# ── 价格类型映射 ──────────────────────────────────────
PRICE_TYPE_MAP = {
    "强": "liq", "强平": "liq", "爆": "liq", "liq": "liq",
    "限价": "limit", "挂单": "limit", "limit": "limit",
}

# ── 移动止盈档位映射（默认中等）──────────────────────
TRAILING_MAP = {
    "保": "conservative", "保守": "conservative",
    "中": "moderate", "中等": "moderate",
    "激": "aggressive", "激进": "aggressive",
    "翻": "double", "翻倍": "double",
}

# ── 滚仓档位映射（默认2档）───────────────────────────
ROLL_MAP = {
    "1": 1, "保": 1, "保守": 1,
    "2": 2, "中": 2, "中等": 2,
    "3": 3, "激": 3, "激进": 3,
}

# ── 常见币种别名 ──────────────────────────────────────
SYMBOL_ALIAS = {
    "比特币": "BTC", "以太": "ETH", "以太坊": "ETH",
    "狗狗": "DOGE", "狗": "DOGE", "猫猫": "MEW",
}


def parse(text: str) -> dict | None:
    """
    把自然语言指令解析成标准 JSON。
    返回 dict 或 None（非交易指令）。

    示例：
      "开多 BTC 10X 100"   → {action:open_long, symbol:BTCUSDT, leverage:10, usdt:100, ...}
      "平 ETH"             → {action:close, symbol:ETHUSDT, ...}
      "移 激 SOL"          → {action:trailing, tier:aggressive, symbol:SOLUSDT, ...}
      "zc 开空 SOL 5x 50"  → {action:open_short, margin_mode:isolated, ...}
    """
    tokens = text.strip().split()
    if not tokens:
        return None

    result = {
        "platform": "binance",      # 默认币安
        "margin_mode": "cross",     # 默认全仓
        "price_type": "market",     # 默认市价
        "action": None,
        "symbol": None,
        "leverage": None,
        "usdt": None,
        "price": None,
        "trailing_tier": "moderate",  # 默认中等
        "roll_tier": 2,               # 默认2档
    }

    unmatched = []

    for token in tokens:
        t = token.lower().strip()

        # 平台
        if t in PLATFORM_MAP:
            result["platform"] = PLATFORM_MAP[t]
            continue

        # 仓位模式
        if t in MARGIN_MAP:
            result["margin_mode"] = MARGIN_MAP[t]
            continue

        # 价格类型
        if t in PRICE_TYPE_MAP:
            result["price_type"] = PRICE_TYPE_MAP[t]
            continue

        # 动作（优先级高，先匹配）
        if t in ACTION_MAP:
            result["action"] = ACTION_MAP[t]
            continue

        # 移动止盈档位
        if t in TRAILING_MAP and result["action"] == "trailing":
            result["trailing_tier"] = TRAILING_MAP[t]
            continue

        # 滚仓档位
        if t in ROLL_MAP and result["action"] == "roll":
            result["roll_tier"] = ROLL_MAP[t]
            continue

        # 杠杆：10x / 10X / 10倍
        lev_match = re.match(r"^(\d+)[xX倍]$", token)
        if lev_match:
            result["leverage"] = int(lev_match.group(1))
            continue

        # 金额：纯数字 → USDT
        if re.match(r"^\d+(\.\d+)?$", token):
            result["usdt"] = float(token)
            continue

        # 限价价格（含小数，较大数字）
        if re.match(r"^\d+\.\d+$", token) and float(token) > 1000:
            result["price"] = float(token)
            continue

        unmatched.append(token.upper())

    # 识别标的（未匹配的大写词当作币种）
    for word in unmatched:
        sym = SYMBOL_ALIAS.get(word, word)
        # 自动补 USDT 后缀
        if not sym.endswith("USDT") and not sym.endswith("BUSD"):
            sym = sym + "USDT"
        result["symbol"] = sym
        break

    # 没识别到动作 → 不是交易指令
    if result["action"] is None:
        return None

    return result


# ── 快速测试 ──────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        "开多 BTC 10X 100",
        "zc 开空 SOL 5x 50",
        "平 ETH",
        "移 激 BTC",
        "滚 3 SOL",
        "币安 开多 比特币 20x 200",
        "hl 开空 ETH 10x 100 限价 3000",
    ]
    for t in tests:
        print(f"输入: {t}")
        print(f"解析: {parse(t)}")
        print()
