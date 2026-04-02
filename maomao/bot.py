#!/usr/bin/env python3
"""
双管道核心 v4.0
- 模型更新到4.6系列
- 立刻响应读秒（独立计时器，实时跳动）
- 完成后删除计时消息，发干净回复
- 执行步骤实时显示
- Vision读图
"""
import os, json, logging, asyncio, time, base64, io
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

load_dotenv(Path(__file__).parent / ".env")
BOT_TOKEN     = os.environ["BOT_TOKEN"]
ADMIN_ID      = int(os.environ["ADMIN_ID"])
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
BOT_NAME      = os.getenv("BOT_NAME", "大猫")
BOT_DIR       = os.getenv("BOT_DIR", "damao")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT",
    "你是大猫，日本VPS的运维助手。主人是乌鸦。"
    "职责：VPS运维、Bot部署、日志排查。简洁直接，中文回复。"
)

# ── 4.6系列模型 ───────────────────────────────────────────
MODELS = [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5-20251001",
]

MODE_FILE = Path(f"/root/{BOT_DIR}/data/mode.json")
MODE_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"/root/{BOT_DIR}/logs/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
#  状态管理
# ══════════════════════════════════════════════════════════
def load_state() -> dict:
    try:
        s = json.loads(MODE_FILE.read_text())
        if "model" not in s:
            s["model"] = MODELS[0]
        return s
    except Exception:
        return {"mode": "api", "model": MODELS[0]}

def save_state(state: dict):
    MODE_FILE.write_text(json.dumps(state, ensure_ascii=False))

def get_mode() -> str:
    return load_state().get("mode", "api")

def get_model() -> str:
    m = load_state().get("model", MODELS[0])
    return m if m in MODELS else MODELS[0]

def mode_label(mode: str) -> str:
    return "🔑 API" if mode == "api" else "📦 订阅"

def model_short(model: str) -> str:
    return model.replace("claude-", "").replace("-20251001", "")

def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            return
        return await func(update, ctx)
    return wrapper


# ══════════════════════════════════════════════════════════
#  核心：实时读秒 + 干净回复
# ══════════════════════════════════════════════════════════
async def ask_with_timer(update: Update, gen_coro):
    """
    1. 立刻发计时消息
    2. 后台计时器每秒更新
    3. gen_coro 完成后取消计时
    4. 删除计时消息
    5. 发干净回复
    """
    mode = get_mode()
    model = get_model()
    icon = "🔑" if mode == "api" else "📦"

    # 立刻弹出计时消息
    thinking_msg = await update.message.reply_text(
        f"{icon} {model_short(model)} · 思考中... 0s"
    )
    start_time = time.time()
    step_info = {"text": "思考中"}  # 可变步骤文字

    # 后台计时任务
    async def ticker():
        while True:
            await asyncio.sleep(1)
            elapsed = int(time.time() - start_time)
            try:
                await thinking_msg.edit_text(
                    f"{icon} {model_short(model)} · {step_info['text']}... {elapsed}s"
                )
            except Exception:
                pass

    ticker_task = asyncio.create_task(ticker())

    full_text = ""
    try:
        async for chunk in gen_coro(step_info):
            full_text += chunk
    except Exception as e:
        full_text = f"⚠️ 错误: {e}"
    finally:
        ticker_task.cancel()

    # 删除计时消息
    try:
        await thinking_msg.delete()
    except Exception:
        pass

    # 发干净回复（超长分段）
    if not full_text.strip():
        full_text = "（无输出）"

    chunks = [full_text[i:i+4000] for i in range(0, len(full_text), 4000)]
    for chunk in chunks:
        await update.message.reply_text(chunk)


# ══════════════════════════════════════════════════════════
#  API模式生成器
# ══════════════════════════════════════════════════════════
async def api_gen(prompt: str, image_b64: str = None):
    """返回异步生成器工厂（接受step_info）"""
    async def _gen(step_info: dict):
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        if image_b64:
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_b64,
                    }
                },
                {"type": "text", "text": prompt or "请描述这张图片"}
            ]
        else:
            content = prompt

        step_info["text"] = "调用API"
        loop = asyncio.get_event_loop()
        queue = asyncio.Queue()

        def _stream():
            try:
                with client.messages.stream(
                    model=get_model(),
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": content}],
                ) as stream:
                    step_info["text"] = "生成回复"
                    for text in stream.text_stream:
                        loop.call_soon_threadsafe(queue.put_nowait, text)
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, f"\n⚠️ {e}")
                loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(None, _stream)

        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk

    return _gen


# ══════════════════════════════════════════════════════════
#  订阅模式生成器
# ══════════════════════════════════════════════════════════
async def subscription_gen(prompt: str):
    """返回异步生成器工厂"""
    async def _gen(step_info: dict):
        step_info["text"] = "调用Claude Code"

        # 用stream-json+verbose获取工具步骤
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--append-system-prompt", SYSTEM_PROMPT,
            "--output-format", "stream-json",
            "--verbose",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        result_text = []
        async for line in proc.stdout:
            raw = line.decode("utf-8").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
                t = data.get("type", "")

                # 工具调用步骤
                if t == "tool_use":
                    tool_name = data.get("name", "工具")
                    step_info["text"] = f"执行 {tool_name}"

                # 文字输出
                elif t == "text":
                    result_text.append(data.get("text", ""))
                elif t == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        result_text.append(delta.get("text", ""))

                # 工具结束
                elif t == "tool_result":
                    step_info["text"] = "处理结果"

            except json.JSONDecodeError:
                pass

        await proc.wait()

        if proc.returncode != 0:
            err = await proc.stderr.read()
            yield f"⚠️ claude CLI错误: {err.decode()[:200]}"
            return

        full = "".join(result_text).strip()
        if full:
            yield full
        else:
            # 回退：重新用text格式跑一次
            proc2 = await asyncio.create_subprocess_exec(
                "claude", "-p", prompt,
                "--append-system-prompt", SYSTEM_PROMPT,
                "--output-format", "text",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout2, stderr2 = await proc2.communicate()
            yield stdout2.decode("utf-8").strip() or "（无输出）"

    return _gen


# ══════════════════════════════════════════════════════════
#  统一入口
# ══════════════════════════════════════════════════════════
async def ask_claude(update: Update, prompt: str, image_b64: str = None):
    mode = get_mode()
    if mode == "api":
        if not ANTHROPIC_KEY:
            await update.message.reply_text("⚠️ ANTHROPIC_API_KEY 未设置")
            return
        gen_coro = await api_gen(prompt, image_b64)
    else:
        gen_coro = await subscription_gen(prompt)

    await ask_with_timer(update, gen_coro)


# ══════════════════════════════════════════════════════════
#  命令处理器
# ══════════════════════════════════════════════════════════
@admin_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    await update.message.reply_text(
        f"{'🐱' if BOT_NAME=='大猫' else '🐾'} <b>{BOT_NAME} v4.0</b>\n\n"
        f"模式: {mode_label(state['mode'])}\n"
        f"模型: <code>{state['model']}</code>\n\n"
        f"/cc — 切换模式\n"
        f"/model — 切换模型\n"
        f"/help — 全部命令",
        parse_mode=ParseMode.HTML,
    )

@admin_only
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    await update.message.reply_text(
        f"<b>{BOT_NAME}命令</b>\n\n"
        f"模式: {mode_label(state['mode'])} | 模型: <code>{model_short(state['model'])}</code>\n\n"
        f"/cc     — 切换 API ↔ 订阅\n"
        f"/model  — 循环切换模型\n"
        f"/mode   — 查看状态\n"
        f"/status — 三Bot服务状态\n"
        f"/ping   — 心跳\n"
        f"/log    — 查看日志\n\n"
        f"<i>发文字 = 问{BOT_NAME}\n发图片 = Vision读图</i>",
        parse_mode=ParseMode.HTML,
    )

@admin_only
async def cmd_cc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    old = state["mode"]
    new = "subscription" if old == "api" else "api"
    state["mode"] = new
    save_state(state)
    await update.message.reply_text(f"✅ {mode_label(old)} → {mode_label(new)}")

@admin_only
async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    cur = state.get("model", MODELS[0])
    try:
        idx = MODELS.index(cur)
    except ValueError:
        idx = 0
    next_model = MODELS[(idx + 1) % len(MODELS)]
    state["model"] = next_model
    save_state(state)
    await update.message.reply_text(
        f"✅ 模型已切换\n\n"
        f"`{model_short(cur)}`\n↓\n`{model_short(next_model)}`",
        parse_mode=ParseMode.MARKDOWN,
    )

@admin_only
async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import shutil
    state = load_state()
    api_ok = "✅" if ANTHROPIC_KEY else "❌"
    cli_ok = "✅" if shutil.which("claude") else "⚠️"
    await update.message.reply_text(
        f"<b>{BOT_NAME} 状态</b>\n\n"
        f"模式: {mode_label(state['mode'])}\n"
        f"模型: <code>{state['model']}</code>\n\n"
        f"🔑 API可用: {api_ok}\n"
        f"📦 订阅可用: {cli_ok}",
        parse_mode=ParseMode.HTML,
    )

@admin_only
async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    await update.message.reply_text(
        f"🏓 {BOT_NAME}存活\n"
        f"{mode_label(state['mode'])} · `{model_short(state['model'])}`",
        parse_mode=ParseMode.MARKDOWN,
    )

@admin_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import subprocess as sp
    lines = []
    for svc in ["damao", "maomao", "baobao"]:
        r = sp.run(["systemctl", "is-active", svc], capture_output=True, text=True)
        icon = "✅" if r.stdout.strip() == "active" else "❌"
        lines.append(f"{icon} {svc}: {r.stdout.strip()}")
    await update.message.reply_text(
        "<b>服务状态</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )

@admin_only
async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        lines = Path(f"/root/{BOT_DIR}/logs/bot.log").read_text().splitlines()[-50:]
        text = "\n".join(lines) or "（日志为空）"
        await update.message.reply_text(
            f"<pre>{text[-3500:]}</pre>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"读取日志失败: {e}")

@admin_only
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ask_claude(update, update.message.text)

@admin_only
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    image_b64 = base64.b64encode(buf.read()).decode()
    caption = update.message.caption or "请描述这张图片"
    await ask_claude(update, caption, image_b64)

@admin_only
async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔄 {BOT_NAME} 重启中...")
    import subprocess
    subprocess.Popen(["systemctl", "restart", BOT_DIR])

@admin_only
async def cmd_stop_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🛑 {BOT_NAME} 关闭中...")
    import subprocess
    subprocess.Popen(["systemctl", "stop", BOT_DIR])

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception:", exc_info=ctx.error)

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",   "状态"),
        BotCommand("help",    "帮助"),
        BotCommand("cc",      "切换API/订阅"),
        BotCommand("model",   "切换模型"),
        BotCommand("mode",    "查看状态"),
        BotCommand("status",  "三Bot服务状态"),
        BotCommand("ping",    "心跳"),
        BotCommand("log",     "查看日志"),
        BotCommand("restart", "重启本Bot"),
        BotCommand("stop",    "关闭本Bot"),
    ])
    state = load_state()
    logger.info(f"{BOT_NAME} v4.0 | {mode_label(state['mode'])} | {state['model']}")
    # 上线通知
    try:
        await app.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"✅ <b>{BOT_NAME} 上线</b>\n\n"
                f"模式: {mode_label(state['mode'])}\n"
                f"模型: <code>{state['model']}</code>"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning(f"上线通知失败: {e}")

def main():
    logger.info(f"=== {BOT_NAME} v4.0 启动 ===")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("cc",     cmd_cc))
    app.add_handler(CommandHandler("model",  cmd_model))
    app.add_handler(CommandHandler("mode",   cmd_mode))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ping",   cmd_ping))
    app.add_handler(CommandHandler("log",    cmd_log))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("stop",    cmd_stop_bot))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_photo))
    app.add_error_handler(error_handler)
    app.job_queue.run_repeating(
        lambda ctx: logger.info(f"[heartbeat] {BOT_NAME}alive"),
        interval=300, first=10
    )
    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        # 下线通知（同步发送）
        import httpx
        state = load_state()
        try:
            httpx.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": ADMIN_ID,
                    "text": f"🔴 <b>{BOT_NAME} 下线</b>",
                    "parse_mode": "HTML",
                },
                timeout=5,
            )
        except Exception:
            pass

if __name__ == "__main__":
    main()
