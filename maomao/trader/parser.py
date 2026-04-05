# 交易指令解析器 — 关键词 → 标准JSON
# v1.1 修复: 数字上下文感知 / 止盈止损价格 / 平多平空方向
import re

PLATFORM_MAP = {
    "bn": "binance", "binance": "binance", "币安": "binance",
    "hl": "hl", "hyperliquid": "hl",
}

ACTION_MAP = {
    "开多": "open_long", "做多": "open_long", "多": "open_long", "long": "open_long",
    "开空": "open_short", "做空": "open_short", "空": "open_short", "short": "open_short",
    "加": "add", "加仓": "add", "补仓": "add",
    "平": "close", "平仓": "close", "全平": "close", "清仓": "close",
    "平多": "close_long", "平空": "close_short",
    "滚": "roll", "滚仓": "roll",
    "移": "trailing", "移动止盈": "trailing",
    "买": "buy", "买入": "buy", "buy": "buy",
    "卖": "sell", "卖出": "sell", "sell": "sell",
    "划转": "transfer", "transfer": "transfer",
    "止盈": "tp", "tp": "tp",
    "止损": "sl", "sl": "sl",
}

MARGIN_MAP = {
    "全": "cross", "全仓": "cross", "cross": "cross",
    "zc": "isolated", "逐仓": "isolated", "isolated": "isolated",
}

# 这些关键词后面跟的数字 → 归 price
PRICE_TYPE_MAP = {
    "强": "liq", "强平": "liq", "爆": "liq", "liq": "liq",
    "限价": "limit", "挂单": "limit", "limit": "limit",
}

TRAILING_MAP = {
    "保": "conservative", "保守": "conservative",
    "中": "moderate", "中等": "moderate",
    "激": "aggressive", "激进": "aggressive",
    "翻": "double", "翻倍": "double",
}

ROLL_MAP = {
    "1": 1, "保": 1, "保守": 1,
    "2": 2, "中": 2, "中等": 2,
    "3": 3, "激": 3, "激进": 3,
}

SYMBOL_ALIAS = {
    "比特币": "BTC", "以太": "ETH", "以太坊": "ETH",
    "狗狗": "DOGE", "狗": "DOGE", "猫猫": "MEW",
}


def parse(text: str) -> dict | None:
    tokens = text.strip().split()
    if not tokens:
        return None

    result = {
        "platform": "binance",
        "margin_mode": "cross",
        "price_type": "market",
        "action": None,
        "symbol": None,
        "leverage": None,
        "usdt": None,
        "price": None,
        "trailing_tier": "moderate",
        "roll_tier": 2,
    }

    unmatched = []
    # 关键修复: 上下文标记 — 下一个数字应归 price 而非 usdt
    next_num_is_price = False

    for token in tokens:
        t = token.lower().strip()

        # ---- 平台 ----
        if t in PLATFORM_MAP:
            result["platform"] = PLATFORM_MAP[t]
            continue

        # ---- 仓位模式 ----
        if t in MARGIN_MAP:
            result["margin_mode"] = MARGIN_MAP[t]
            continue

        # ---- 价格类型(限价/强平) → 后面的数字归 price ----
        if t in PRICE_TYPE_MAP:
            result["price_type"] = PRICE_TYPE_MAP[t]
            next_num_is_price = True
            continue

        # ---- 动作 ----
        if t in ACTION_MAP:
            result["action"] = ACTION_MAP[t]
            # 止盈/止损动作后面的数字也归 price
            if result["action"] in ("tp", "sl"):
                next_num_is_price = True
            continue

        # ---- 移动止盈档位 ----
        if t in TRAILING_MAP and result["action"] == "trailing":
            result["trailing_tier"] = TRAILING_MAP[t]
            continue

        # ---- 滚仓档位 ----
        if t in ROLL_MAP and result["action"] == "roll":
            result["roll_tier"] = ROLL_MAP[t]
            continue

        # ---- 杠杆 (10x / 20倍) ----
        lev_match = re.match(r"^(\d+)[xX倍]$", token)
        if lev_match:
            result["leverage"] = int(lev_match.group(1))
            continue

        # ---- 数字: 根据上下文分配到 price 或 usdt ----
        if re.match(r"^\d+(\.\d+)?$", token):
            num = float(token)
            if next_num_is_price:
                result["price"] = num
                next_num_is_price = False
            else:
                result["usdt"] = num
            continue

        # ---- 未匹配 → 候选 symbol ----
        unmatched.append(token.upper())

    # 从未匹配词里取 symbol
    for word in unmatched:
        sym = SYMBOL_ALIAS.get(word, word)
        if not sym.endswith("USDT") and not sym.endswith("BUSD"):
            sym = sym + "USDT"
        result["symbol"] = sym
        break

    if result["action"] is None:
        return None

    return result


if __name__ == "__main__":
    tests = [
        "开多 BTC 10X 100",
        "开多 BTC 10x 100 限价 85000",
        "zc 开空 SOL 5x 50",
        "平 ETH",
        "平多 ETH",
        "平空 BTC",
        "止盈 BTC 90000",
        "止损 ETH 2800",
        "移 激 BTC",
        "滚 3 SOL",
        "币安 开多 比特币 20x 200",
        "hl 开空 ETH 10x 100 限价 3000",
        "强平 开多 BTC 10x 100",
    ]
    for t in tests:
        print(f"输入: {t}")
        print(f"解析: {parse(t)}")
        print()
