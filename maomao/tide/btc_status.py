#!/usr/bin/env python3
"""小刃 · BTC 30分钟状态推送"""
import json
import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.request
import urllib.parse
import yaml

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_PATH = BASE_DIR / "logs" / "main.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("tide.btc_status")

BJ = timezone(timedelta(hours=8))


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_env(env_file: str) -> dict:
    env = {}
    try:
        for line in Path(env_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except Exception as e:
        log.error(f"读取 env 文件失败: {e}")
    return env


def fetch_btc_price() -> tuple[float, float]:
    """返回 (当前价, 24h涨幅%)"""
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=BTCUSDT"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        price = float(data["lastPrice"])
        pct = float(data["priceChangePercent"])
        return price, pct
    except Exception as e:
        log.error(f"获取 BTC 价格失败: {e}")
        raise


def get_zone(price: float, zones: list) -> dict:
    for z in zones:
        if z["lower"] <= price < z["upper"]:
            return z
    return {"name": "unknown", "label": "未知区段", "action": "?", "emoji": "❓"}


ACTION_DESC = {
    "FORCE_FLAT": "⚠️ 全平离场",
    "REDUCE_70": "减仓 70%",
    "REDUCE_50": "减仓 50%",
    "REDUCE_30": "减仓 30%",
    "NO_ACTION": "观望持仓",
    "ADD_1X": "加仓 1x base",
    "ADD_1_5X": "加仓 1.5x base",
    "ADD_2X": "加仓 2x base",
    "ADD_3X": "加仓 3x base ⚠️",
}


def format_card(price: float, pct: float, zone: dict, cfg: dict) -> str:
    box = cfg["box"]
    now_bj = datetime.now(BJ).strftime("%m/%d %H:%M")
    arrow = "▲" if pct >= 0 else "▼"
    pct_str = f"{arrow}{abs(pct):.2f}%"
    action = ACTION_DESC.get(zone["action"], zone["action"])

    lines = [
        f"🌊 <b>小刃 潮汐-BTC实时状态</b>",
        f"━━━━━━━━━━━━━━━━━━━",
        f"💰 当前价格　<b>${price:,.0f}</b>　{pct_str}(24h)",
        f"📍 所在区段　{zone['emoji']} {zone['label']}",
        f"🎯 策略信号　{action}",
        f"",
        f"─── 关键位置 ───",
        f"🚨 母箱上沿　${box['mother']['upper']:,}",
        f"🔴 对岸上沿　${box['small']['upper']:,}",
        f"⚖️ 中心轴　　${box['small']['center']:,}",
        f"🟢 小箱下沿　${box['small']['lower']:,}",
        f"🚨 母箱下沿　${box['mother']['lower']:,}",
        f"",
        f"⏰ {now_bj} BJ",
    ]
    return "\n".join(lines)


def send_tg(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": True,
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        raise RuntimeError(f"TG 发送失败: {resp}")


def run_once():
    cfg = load_config()
    tg_cfg = cfg["notifications"]["telegram"]
    env = load_env(tg_cfg["env_file"])
    token = env.get(tg_cfg["bot_token_key"])
    chat_id = env.get(tg_cfg["chat_id_key"])
    if not token or not chat_id:
        raise RuntimeError("TG token 或 chat_id 未找到")

    price, pct = fetch_btc_price()
    zone = get_zone(price, cfg["zones"])
    card = format_card(price, pct, zone, cfg)
    send_tg(token, chat_id, card)
    log.info(f"BTC ${price:,.0f} 区段={zone['name']} 卡片已推送")


if __name__ == "__main__":
    run_once()
