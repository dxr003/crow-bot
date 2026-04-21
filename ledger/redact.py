"""敏感字段脱敏。

覆盖：
- Telegram bot token: `1234567890:AAFxxx...` → `***TG_TOKEN***`
- URL 里嵌的 TG token: `bot1234567890:AAFxxx/sendMessage` → `bot***TG_TOKEN***/sendMessage`
- 币安 API key 前缀（32+ hex/base64 风格长串）→ `***API_KEY***`
- 私钥片段（0x + 64 hex）→ `***PRIVATE_KEY***`

脱敏只作用在字符串字段；结构化字段（dict/list）递归处理。
"""
from __future__ import annotations

import re
from typing import Any

# Telegram bot token 两种形态：
#   1) URL 里嵌入：`bot1234567890:AAFxxx/sendMessage` — bot 前缀紧贴 token
#   2) 独立出现：" 1234567890:AAFxxx " — 前后非字母数字
# \b 在 `bot` 与数字之间不触发（都是 word char），所以拆两条正则分别处理。
_TG_TOKEN_IN_URL_RE = re.compile(r"(bot)(\d{8,12}:[A-Za-z0-9_\-]{30,})")
_TG_TOKEN_STANDALONE_RE = re.compile(
    r"(?<![A-Za-z0-9])\d{8,12}:[A-Za-z0-9_\-]{30,}(?![A-Za-z0-9])"
)

# 私钥 / 合约地址（0x + 64 hex = 256 bit；0x + 40 hex = 160 bit 地址）
_PRIVATE_KEY_RE = re.compile(r"\b0x[a-fA-F0-9]{64}\b")
_ETH_ADDR_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")

# 币安 API key 典型 64 位 base64-ish
_BINANCE_KEY_RE = re.compile(r"\b[A-Za-z0-9]{64}\b")


def scrub(value: Any) -> Any:
    """递归脱敏。dict/list 保持结构，只替换字符串值。"""
    if isinstance(value, str):
        return _scrub_str(value)
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        cls = type(value)
        return cls(scrub(v) for v in value)
    return value


def _scrub_str(s: str) -> str:
    s = _TG_TOKEN_IN_URL_RE.sub(r"\1***TG_TOKEN***", s)
    s = _TG_TOKEN_STANDALONE_RE.sub("***TG_TOKEN***", s)
    s = _PRIVATE_KEY_RE.sub("***PRIVATE_KEY***", s)
    # 地址用半截保留：0x1234...abcd
    s = _ETH_ADDR_RE.sub(lambda m: m.group(0)[:6] + "..." + m.group(0)[-4:], s)
    # 币安 key 保留前后 4 位
    s = _BINANCE_KEY_RE.sub(lambda m: m.group(0)[:4] + "***" + m.group(0)[-4:], s)
    return s
