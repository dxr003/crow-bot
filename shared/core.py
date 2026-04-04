#!/usr/bin/env python3
"""
共享底盘 v4.1 — 双管道核心引擎
大猫和毛毛共用，通过参数区分行为。
此文件冻结后不再修改，新功能通过模块挂载。
"""
import os, json, logging, asyncio, time, base64, io
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

MODELS = [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5-20251001",
]

def _state_file(bot_dir):
    p = Path(f"/root/{bot_dir}/data/mode.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def load_state(bot_dir):
    try:
        s = json.loads(_state_file(bot_dir).read_text())
        if "model" not in s: s["model"] = MODELS[0]
        return s
    except Exception:
        return {"mode": "api", "model": MODELS[0]}

def save_state(bot_dir, state):
    _state_file(bot_dir).write_text(json.dumps(state, ensure_ascii=False))

def mode_label(mode):
    return "🔑 API" if mode == "api" else "📦 订阅"

def model_short(model):
    return model.replace("claude-", "").replace("-20251001", "")

async def ask_with_timer(update, gen_coro, mode, model):
    icon = "🔑" if mode == "api" else "📦"
    thinking_msg = await update.message.reply_text(f"{icon} {model_short(model)} · 思考中... 0s")
    start_time = time.time()
    step_info = {"text": "思考中"}

    async def ticker():
        while True:
            await asyncio.sleep(1)
            elapsed = int(time.time() - start_time)
            try:
                await thinking_msg.edit_text(f"{icon} {model_short(model)} · {step_info['text']}... {elapsed}s")
            except Exception: pass

    ticker_task = asyncio.create_task(ticker())
    full_text = ""
    try:
        async for chunk in gen_coro(step_info):
            full_text += chunk
    except Exception as e:
        full_text = f"⚠️ 错误: {e}"
    finally:
        ticker_task.cancel()

    try: await thinking_msg.delete()
    except Exception: pass

    if not full_text.strip(): full_text = "（无输出）"
    chunks = [full_text[i:i+4000] for i in range(0, len(full_text), 4000)]
    for chunk in chunks:
        await update.message.reply_text(chunk)

async def api_gen(prompt, api_key, model, system_prompt, image_b64=None):
    async def _gen(step_info):
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        if image_b64:
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": prompt or "请描述这张图片"}
            ]
        else:
            content = prompt
        step_info["text"] = "调用API"
        loop = asyncio.get_event_loop()
        queue = asyncio.Queue()
        def _stream():
            try:
                with client.messages.stream(model=model, max_tokens=4096, system=system_prompt,
                    messages=[{"role": "user", "content": content}]) as stream:
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
            if chunk is None: break
            yield chunk
    return _gen

async def subscription_gen(prompt, system_prompt, claude_add_dir=None):
    async def _gen(step_info):
        step_info["text"] = "调用Claude Code"
        cmd = ["claude", "-p", prompt]
        if claude_add_dir: cmd.extend(["--add-dir", claude_add_dir])
        cmd.extend(["--append-system-prompt", system_prompt, "--output-format", "stream-json", "--verbose"])
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        result_text = []
        async for line in proc.stdout:
            raw = line.decode("utf-8").strip()
            if not raw: continue
            try:
                data = json.loads(raw)
                t = data.get("type", "")
                if t == "tool_use": step_info["text"] = f"执行 {data.get('name', '工具')}"
                elif t == "text": result_text.append(data.get("text", ""))
                elif t == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta": result_text.append(delta.get("text", ""))
                elif t == "tool_result": step_info["text"] = "处理结果"
            except json.JSONDecodeError: pass
        await proc.wait()
        if proc.returncode != 0:
            err = await proc.stderr.read()
            yield f"⚠️ claude CLI错误: {err.decode()[:200]}"
            return
        full = "".join(result_text).strip()
        if full:
            yield full
        else:
            cmd2 = ["claude", "-p", prompt]
            if claude_add_dir: cmd2.extend(["--add-dir", claude_add_dir])
            cmd2.extend(["--append-system-prompt", system_prompt, "--output-format", "text"])
            proc2 = await asyncio.create_subprocess_exec(*cmd2, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout2, _ = await proc2.communicate()
            yield stdout2.decode("utf-8").strip() or "（无输出）"
    return _gen

def create_and_run_bot(env_path, claude_add_dir=None):
    load_dotenv(env_path)
    bot_token     = os.environ["BOT_TOKEN"]
    admin_id      = int(os.environ["ADMIN_ID"])
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    bot_name      = os.getenv("BOT_NAME", "Bot")
    bot_dir       = os.getenv("BOT_DIR", "bot")
    system_prompt = os.getenv("SYSTEM_PROMPT", "你是AI助手。")

    log_dir = Path(f"/root/{bot_dir}/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO,
        handlers=[logging.StreamHandler(), logging.FileHandler(log_dir / "bot.log", encoding="utf-8")])
    logger = logging.getLogger(bot_name)

    def admin_only(func):
        async def wrapper(update, ctx):
            if update.effective_user.id != admin_id: return
            return await func(update, ctx)
        return wrapper

    async def ask_claude(update, prompt, image_b64=None):
        state = load_state(bot_dir)
        mode = state.get("mode", "api")
        model = state.get("model", MODELS[0])
        if mode == "api":
            if not anthropic_key:
                await update.message.reply_text("⚠️ ANTHROPIC_API_KEY 未设置")
                return
            gen = await api_gen(prompt, anthropic_key, model, system_prompt, image_b64)
        else:
            gen = await subscription_gen(prompt, system_prompt, claude_add_dir)
        await ask_with_timer(update, gen, mode, model)

    @admin_only
    async def cmd_start(update, ctx):
        state = load_state(bot_dir)
        await update.message.reply_text(
            f"{'🐱' if bot_name=='大猫' else '🐾'} <b>{bot_name} v4.1</b>\n\n"
            f"模式: {mode_label(state['mode'])}\n模型: <code>{state['model']}</code>\n\n"
            f"/cc — 切换模式\n/model — 切换模型\n/help — 全部命令",
            parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_help(update, ctx):
        state = load_state(bot_dir)
        await update.message.reply_text(
            f"<b>{bot_name}命令</b>\n\n"
            f"模式: {mode_label(state['mode'])} | 模型: <code>{model_short(state['model'])}</code>\n\n"
            f"/cc     — 切换 API ↔ 订阅\n/model  — 循环切换模型\n"
            f"/mode   — 查看状态\n/status — 三Bot服务状态\n"
            f"/ping   — 心跳\n/log    — 查看日志\n\n"
            f"<i>发文字 = 问{bot_name}\n发图片 = Vision读图</i>",
            parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_cc(update, ctx):
        state = load_state(bot_dir)
        old = state["mode"]
        new = "subscription" if old == "api" else "api"
        state["mode"] = new
        save_state(bot_dir, state)
        await update.message.reply_text(f"✅ {mode_label(old)} → {mode_label(new)}")

    @admin_only
    async def cmd_model(update, ctx):
        state = load_state(bot_dir)
        cur = state.get("model", MODELS[0])
        try: idx = MODELS.index(cur)
        except ValueError: idx = 0
        nxt = MODELS[(idx + 1) % len(MODELS)]
        state["model"] = nxt
        save_state(bot_dir, state)
        await update.message.reply_text(f"✅ 模型已切换\n\n`{model_short(cur)}`\n↓\n`{model_short(nxt)}`", parse_mode=ParseMode.MARKDOWN)

    @admin_only
    async def cmd_mode(update, ctx):
        import shutil
        state = load_state(bot_dir)
        api_ok = "✅" if anthropic_key else "❌"
        cli_ok = "✅" if shutil.which("claude") else "⚠️"
        await update.message.reply_text(
            f"<b>{bot_name} 状态</b>\n\n模式: {mode_label(state['mode'])}\n模型: <code>{state['model']}</code>\n\n"
            f"🔑 API可用: {api_ok}\n📦 订阅可用: {cli_ok}", parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_ping(update, ctx):
        state = load_state(bot_dir)
        await update.message.reply_text(f"🏓 {bot_name}存活\n{mode_label(state['mode'])} · `{model_short(state['model'])}`", parse_mode=ParseMode.MARKDOWN)

    @admin_only
    async def cmd_status(update, ctx):
        import subprocess as sp
        lines = []
        for svc in ["damao", "maomao", "baobao"]:
            r = sp.run(["systemctl", "is-active", svc], capture_output=True, text=True)
            icon = "✅" if r.stdout.strip() == "active" else "❌"
            lines.append(f"{icon} {svc}: {r.stdout.strip()}")
        await update.message.reply_text("<b>服务状态</b>\n\n" + "\n".join(lines), parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_log(update, ctx):
        try:
            lines = (log_dir / "bot.log").read_text().splitlines()[-50:]
            text = "\n".join(lines) or "（日志为空）"
            await update.message.reply_text(f"<pre>{text[-3500:]}</pre>", parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"读取日志失败: {e}")

    @admin_only
    async def handle_text(update, ctx):
        await ask_claude(update, update.message.text)

    @admin_only
    async def handle_photo(update, ctx):
        photo = update.message.photo[-1]
        file = await ctx.bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode()
        await ask_claude(update, update.message.caption or "请描述这张图片", image_b64)

    @admin_only
    async def cmd_restart(update, ctx):
        await update.message.reply_text(f"🔄 {bot_name} 重启中...")
        import subprocess
        subprocess.Popen(["systemctl", "restart", bot_dir])

    @admin_only
    async def cmd_stop_bot(update, ctx):
        await update.message.reply_text(f"🛑 {bot_name} 关闭中...")
        import subprocess
        subprocess.Popen(["systemctl", "stop", bot_dir])

    async def error_handler(update, ctx):
        logger.error("Exception:", exc_info=ctx.error)

    async def post_init(app):
        await app.bot.set_my_commands([
            BotCommand("start","状态"), BotCommand("help","帮助"),
            BotCommand("cc","切换API/订阅"), BotCommand("model","切换模型"),
            BotCommand("mode","查看状态"), BotCommand("status","三Bot服务状态"),
            BotCommand("ping","心跳"), BotCommand("log","查看日志"),
            BotCommand("restart","重启本Bot"), BotCommand("stop","关闭本Bot"),
        ])
        state = load_state(bot_dir)
        logger.info(f"{bot_name} v4.1 | {mode_label(state['mode'])} | {state['model']}")
        try:
            await app.bot.send_message(chat_id=admin_id,
                text=f"✅ <b>{bot_name} 上线</b>\n\n模式: {mode_label(state['mode'])}\n模型: <code>{state['model']}</code>",
                parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"上线通知失败: {e}")

    logger.info(f"=== {bot_name} v4.1 启动 ===")
    app = Application.builder().token(bot_token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cc", cmd_cc))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("stop", cmd_stop_bot))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_photo))
    app.add_error_handler(error_handler)
    app.job_queue.run_repeating(lambda ctx: logger.info(f"[heartbeat] {bot_name} alive"), interval=300, first=10)

    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        import httpx
        try:
            httpx.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": admin_id, "text": f"🔴 <b>{bot_name} 下线</b>", "parse_mode": "HTML"}, timeout=5)
        except Exception: pass
