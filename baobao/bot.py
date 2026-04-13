#!/usr/bin/env python3
"""
贝贝 Bot (@Maoju9_bot) — 播报骨架 v1.0
职责: 接收信号推送、持仓快照、系统健康报告
特点: 单向输出为主，不参与交易决策
"""
import os, logging, requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

load_dotenv(Path(__file__).parent / ".env")
BOT_TOKEN        = os.environ["BOT_TOKEN"]
ADMIN_ID         = int(os.environ["ADMIN_ID"])
BROADCAST_CHAT_ID = os.getenv("BROADCAST_CHAT_ID", "")  # 播报目标频道/群ID

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/root/baobao/logs/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            return
        return await func(update, ctx)
    return wrapper

async def broadcast(app: Application, text: str):
    """核心播报函数，供外部模块调用"""
    if not BROADCAST_CHAT_ID:
        logger.warning("BROADCAST_CHAT_ID 未设置，无法播报")
        return
    try:
        await app.bot.send_message(
            chat_id=BROADCAST_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        logger.info(f"播报成功: {text[:50]}...")
    except Exception as e:
        logger.error(f"播报失败: {e}")

@admin_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = BROADCAST_CHAT_ID or "⚠️ 未设置"
    await update.message.reply_text(
        "📢 <b>贝贝 在线</b>\n\n"
        f"播报目标: <code>{target}</code>\n"
        "状态: ✅ 正常\n\n"
        "/help 查看命令",
        parse_mode=ParseMode.HTML,
    )

@admin_only
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>贝贝命令</b>\n\n"
        "/start   — 状态\n"
        "/ping    — 心跳\n"
        "/test    — 发送测试播报\n"
        "/bc 内容 — 手动播报一条消息\n",
        parse_mode=ParseMode.HTML,
    )

@admin_only
async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong！贝贝存活")

@admin_only
async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await broadcast(ctx.application, "📢 <b>播报测试</b>\n\n这是一条测试消息，贝贝正常运行。")
    await update.message.reply_text("✅ 测试播报已发送")

@admin_only
async def cmd_bc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("用法: /bc 要播报的内容")
        return
    text = " ".join(ctx.args)
    await broadcast(ctx.application, f"📢 {text}")
    await update.message.reply_text("✅ 已播报")

@admin_only
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("请用 /bc 内容 来手动播报，或 /test 测试。")

MIN_CHANGE_PCT = 50  # 24h涨幅门槛

def scan_hot_coins() -> list:
    """扫描币安全市场合约，返回24h涨幅≥门槛的币（排除下架币），按涨幅降序"""
    FAPI = "https://fapi.binance.com"
    try:
        # 拉 exchangeInfo 过滤非TRADING币
        ei_resp = requests.get(f"{FAPI}/fapi/v1/exchangeInfo", timeout=10)
        ei_resp.raise_for_status()
        trading = {s["symbol"] for s in ei_resp.json().get("symbols", []) if s["status"] == "TRADING"}

        resp = requests.get(f"{FAPI}/fapi/v1/ticker/24hr", timeout=10)
        resp.raise_for_status()
        coins = []
        for t in resp.json():
            sym = t["symbol"]
            if not sym.endswith("USDT") or sym not in trading:
                continue
            change = float(t["priceChangePercent"])
            if change >= MIN_CHANGE_PCT:
                coins.append({
                    "symbol": sym,
                    "base": sym.replace("USDT", ""),
                    "change": change,
                    "price": float(t["lastPrice"]),
                    "volume": float(t["quoteVolume"]),
                })
        coins.sort(key=lambda x: x["change"], reverse=True)

        # 批量拉费率
        try:
            fr_resp = requests.get(f"{FAPI}/fapi/v1/premiumIndex", timeout=8)
            fr_resp.raise_for_status()
            fr_map = {x["symbol"]: float(x["lastFundingRate"]) for x in fr_resp.json()}
            for c in coins:
                c["funding"] = fr_map.get(c["symbol"], 0)
        except Exception:
            for c in coins:
                c["funding"] = 0

        return coins
    except Exception as e:
        logger.error(f"涨幅列表扫描失败: {e}")
        return []

def _heat_icon(change: float) -> str:
    if change >= 200: return "🔴"
    if change >= 100: return "🟠"
    if change >= 70:  return "🟡"
    return "🟢"

def _fr_tag(fr: float) -> str:
    if fr < -0.1:  return "🔥空付多"
    if fr > 0.1:   return "⚠️多付空"
    return "➖"

def format_hot_card(coins: list) -> str:
    """格式化实时涨幅卡片"""
    now = datetime.now().strftime("%H:%M")
    if not coins:
        return f"📊 <b>实时涨幅列表</b>  {now}\n\n当前无24h涨幅≥{MIN_CHANGE_PCT}%的币"

    lines = [
        f"📊 <b>实时涨幅列表</b>  {now}",
        f"━━━━━━━━━━━━━━━",
    ]
    for i, c in enumerate(coins[:20], 1):
        vol_m = c["volume"] / 1e6
        fr = c.get("funding", 0) * 100
        icon = _heat_icon(c["change"])
        lines.append(
            f"\n{icon} <b>{c['base']}</b>   <b>+{c['change']:.1f}%</b>\n"
            f"   价格 <code>{c['price']:.4g}</code>  ｜ 成交 <code>{vol_m:.0f}M</code>\n"
            f"   费率 <code>{fr:+.3f}%</code> {_fr_tag(fr)}"
        )
    lines.append(f"\n━━━━━━━━━━━━━━━")
    lines.append(f"共 <b>{len(coins)}</b> 个币  ｜  门槛 24h≥{MIN_CHANGE_PCT}%")
    return "\n".join(lines)

async def job_hot_coins(ctx: ContextTypes.DEFAULT_TYPE):
    """整点推送爆发币卡片到群组"""
    coins = scan_hot_coins()
    card = format_hot_card(coins)
    try:
        await ctx.bot.send_message(
            chat_id=BROADCAST_CHAT_ID,
            text=card,
            parse_mode=ParseMode.HTML,
        )
        logger.info(f"[爆发币] 推送成功，{len(coins)}个币")
    except Exception as e:
        logger.error(f"[爆发币] 推送失败: {e}")

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception:", exc_info=ctx.error)

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "状态"),
        BotCommand("help",  "帮助"),
        BotCommand("ping",  "心跳"),
        BotCommand("test",  "测试播报"),
        BotCommand("bc",    "手动播报"),
    ])

def main():
    logger.info("=== 贝贝启动 v1.0 ===")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("ping",  cmd_ping))
    app.add_handler(CommandHandler("test",  cmd_test))
    app.add_handler(CommandHandler("bc",    cmd_bc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_text))
    app.add_error_handler(error_handler)
    async def _heartbeat(ctx): logger.info("[heartbeat] 播报alive")
    app.job_queue.run_repeating(_heartbeat, interval=300, first=10)
    # 实时涨幅列表（每小时XX:50）
    from datetime import time as dtime
    for h in range(24):
        app.job_queue.run_daily(job_hot_coins, time=dtime(hour=h, minute=50, second=0))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
