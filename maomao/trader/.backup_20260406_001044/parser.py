"""
交易指令解析器 v1.2
关键词 → 标准JSON

v1.2 变更:
  - 复合指令: "做多 SOL 5x 20u 止损 65" → 主动作open_long + sl_price=65
  - "20u" 后缀识别 → usdt=20
  - 止盈/止损跟在开仓后面当附加条件，不覆盖主动作
  - 独立止盈止损仍然正常: "止盈 BTC 90000" → action=tp
"""
import re

PLATFORM_MAP = {
    "bn": "binance", "binance": "binance", "币安": "binance",
    "hl": "hl", "hyperliquid": "hl",
}

# 主动作 — 决定 execute() 走哪个分支
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

# 这些动作是"开仓类"，后面跟止盈止损算附加条件
OPEN_ACTIONS = {"open_long", "open_short", "add"}

MARGIN_MAP = {
    "全": "cross", "全仓": "cross", "cross": "cross",
    "zc": "isolated", "逐仓": "isolated", "isolated": "isolated",
}

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
        "tp_price": None,      # 附加止盈价
        "sl_price": None,      # 附加止损价
        "trailing_tier": "moderate",
        "roll_tier": 2,
    }

    unmatched = []
    # 上下文: 下一个数字归谁
    # "price" = 限价/强平的价格
    # "tp" = 止盈价
    # "sl" = 止损价
    # None = 归 usdt
    next_num_target = None

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

        # ---- 价格类型(限价/强平) → 后面数字归 price ----
        if t in PRICE_TYPE_MAP:
            result["price_type"] = PRICE_TYPE_MAP[t]
            next_num_target = "price"
            continue

        # ---- 动作 ----
        if t in ACTION_MAP:
            mapped = ACTION_MAP[t]

            # 关键逻辑: 如果已有开仓主动作，止盈/止损变成附加条件
            if mapped == "tp" and result["action"] in OPEN_ACTIONS:
                next_num_target = "tp"
                continue
            elif mapped == "sl" and result["action"] in OPEN_ACTIONS:
                next_num_target = "sl"
                continue
            else:
                # 第一个动作 或 独立止盈止损指令
                result["action"] = mapped
                if mapped == "tp":
                    next_num_target = "tp"
                elif mapped == "sl":
                    next_num_target = "sl"
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

        # ---- 金额带u后缀: 20u / 100U → usdt ----
        u_match = re.match(r"^(\d+(?:\.\d+)?)[uU]$", token)
        if u_match:
            result["usdt"] = float(u_match.group(1))
            continue

        # ---- 纯数字: 根据上下文分配 ----
        if re.match(r"^\d+(\.\d+)?$", token):
            num = float(token)
            if next_num_target == "price":
                result["price"] = num
                next_num_target = None
            elif next_num_target == "tp":
                result["tp_price"] = num
                next_num_target = None
            elif next_num_target == "sl":
                result["sl_price"] = num
                next_num_target = None
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


# ============================================================
# 指令前缀检测 — 供 router.py 调用
# ============================================================
# 任何token命中这些词 → 判定为交易指令
TRIGGER_WORDS = set()
TRIGGER_WORDS.update(ACTION_MAP.keys())
TRIGGER_WORDS.update(PLATFORM_MAP.keys())
TRIGGER_WORDS.update(MARGIN_MAP.keys())
# 常见币种也作为触发词
TRIGGER_SYMBOLS = {
    "btc", "eth", "sol", "doge", "bnb", "xrp", "ada", "avax",
    "link", "dot", "matic", "sui", "apt", "arb", "op", "mew",
    "pepe", "wif", "bonk", "floki", "shib", "比特币", "以太", "以太坊",
}
TRIGGER_WORDS.update(TRIGGER_SYMBOLS)


def is_trade_command(text: str) -> bool:
    """快速判断: 文本是否像交易指令(供路由层零token拦截)"""
    tokens = text.strip().lower().split()
    if not tokens:
        return False
    # 前3个token内有交易关键词就判定为交易指令
    for t in tokens[:3]:
        # 去掉可能的u后缀和x后缀再判断
        clean = re.sub(r'[uxX倍]$', '', t)
        if t in TRIGGER_WORDS or clean in TRIGGER_WORDS:
            return True
    return False


if __name__ == "__main__":
    tests = [
        "做多 SOL 5x 20u 止损 65",
        "开多 BTC 10x 100 限价 85000",
        "开多 BTC 10x 100 止盈 95000 止损 78000",
        "做空 ETH 20x 50u 止损 4000 止盈 3200",
        "止盈 BTC 90000",
        "止损 ETH 2800",
        "平 ETH",
        "平多 ETH",
        "平空 BTC",
        "zc 开空 SOL 5x 50",
        "移 激 BTC",
        "滚 3 SOL",
        "做多 btc 5",
    ]
    print("=" * 60)
    print("parser v1.2 测试")
    print("=" * 60)
    for t in tests:
        r = parse(t)
        # 精简输出: 只显示非None/非默认字段
        if r:
            show = {k: v for k, v in r.items()
                    if v is not None and v != "binance" and v != "cross"
                    and v != "market" and v != "moderate" and v != 2}
            print(f"✅ {t}")
            print(f"   → {show}")
        else:
            print(f"❌ {t} → 无法解析")
        print()

    print("=" * 60)
    print("is_trade_command 测试")
    print("=" * 60)
    cmd_tests = [
        ("做多 SOL 5x 20u", True),
        ("btc 做多 100", True),
        ("bn 开多 sol", True),
        ("你好", False),
        ("帮我看看行情", False),
        ("今天天气怎么样", False),
        ("止盈 BTC 90000", True),
        ("平 ETH", True),
    ]
    for text, expected in cmd_tests:
        got = is_trade_command(text)
        mark = "✅" if got == expected else "❌"
        print(f"  {mark} \"{text}\" → {got} (期望 {expected})")
