#!/usr/bin/env python3
"""
共享底盘 v4.2 — 双管道核心引擎
大猫和玄玄共用，通过参数区分行为。
# 底座已固定 v4.2 — 新功能只通过 trader/ 模块挂载，禁止修改此文件
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
        return {"mode": "subscription", "model": MODELS[0]}

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
            actions = step_info.get("actions")
            if actions:
                lines = "\n".join(f"  {a}" for a in actions[-10:])
                txt = f"⚡ {elapsed}s\n{lines}"
            else:
                txt = f"{icon} {model_short(model)} · {step_info['text']}... {elapsed}s"
            try:
                await thinking_msg.edit_text(txt)
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
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"})
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
        mode = state.get("mode", "subscription")
        model = state.get("model", MODELS[0])
        if mode == "api":
            if not anthropic_key:
                await update.message.reply_text("⚠️ ANTHROPIC_API_KEY 未设置")
                return
            gen = await api_gen(prompt, anthropic_key, model, system_prompt, image_b64)
        else:
            if bot_dir == "damao":
                gen = await claudecode_gen(prompt, add_dir=claude_add_dir or "/root", bot_dir=bot_dir)
            elif bot_dir == "maomao":
                gen = await claudecode_gen(prompt, add_dir="/root/maomao", bot_dir=bot_dir)
            else:
                gen = await subscription_gen(prompt, system_prompt, claude_add_dir)
        await ask_with_timer(update, gen, mode, model)

    @admin_only
    async def cmd_start(update, ctx):
        state = load_state(bot_dir)
        await update.message.reply_text(
            f"{'🐱' if bot_name=='大猫' else '🐦' if bot_name=='玄玄' else '🐾'} <b>{bot_name} v4.2</b>\n\n"
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

    _CONFIRM_WORDS = {"确认", "ok", "OK", "好", "是", "确定", "yes", "y", "Y"}
    _CANCEL_WORDS  = {"取消", "cancel", "不", "算了", "不要", "no", "n", "N"}

    @admin_only
    async def handle_text(update, ctx):
        user_text = update.message.text
        if bot_dir == "maomao":
            import sys
            sys.path.insert(0, '/root/maomao')

            # ── 文本确认/取消拦截（优先于 AI 和硬解析）──
            stripped = user_text.strip()
            if stripped in _CONFIRM_WORDS:
                from trader.preview import pop_latest_pending
                order = pop_latest_pending()
                if order is not None:
                    from trader.order import execute
                    from trader.trade_log import log_trade
                    try:
                        result = execute(order)
                        log_trade(order=order, result=result)
                    except Exception as e:
                        result = f"❌ 执行失败: {e}"
                        log_trade(order=order, error=str(e))
                    await update.message.reply_text(result, parse_mode=ParseMode.HTML)
                    return
            elif stripped in _CANCEL_WORDS:
                from trader.preview import pop_latest_pending
                order = pop_latest_pending()
                if order is not None:
                    await update.message.reply_text("❌ 已取消")
                    return

            # ── 硬解析交易指令 ──
            from trader.router import try_trade_command
            trade_result = try_trade_command(user_text)
            if trade_result is not None:
                result_text, uid = trade_result
                await update.message.reply_text(result_text, parse_mode=ParseMode.HTML)
                if uid is None:
                    # 直接动作（撤单等）已执行，记录日志
                    from trader.trade_log import log_trade
                    log_trade(raw_text=user_text, result=result_text)
                if uid is not None:
                    # 需要确认：启动60s超时
                    async def _auto_cancel():
                        import asyncio as _asyncio
                        await _asyncio.sleep(60)
                        from trader.preview import pop_pending
                        if pop_pending(uid) is not None:
                            try:
                                await update.message.reply_text("⏱ 已超时取消")
                            except Exception:
                                pass
                    asyncio.create_task(_auto_cancel())
                return

        await ask_claude(update, user_text)

    async def handle_callback(update, ctx):
        await update.callback_query.answer()

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

    # ── 玄玄专属快捷指令 /1-/6（仅 maomao 注册）──
    @admin_only
    async def cmd_q_positions(update, ctx):
        import sys; sys.path.insert(0, '/root/maomao')
        from trader.exchange import get_positions
        try:
            positions = get_positions()
            if not positions:
                await update.message.reply_text("📭 当前无持仓")
                return
            lines = []
            for p in positions:
                amt  = float(p['positionAmt'])
                side = "多" if amt > 0 else "空"
                entry = float(p['entryPrice'])
                liq   = float(p.get('liquidationPrice', 0))
                upnl  = float(p.get('unRealizedProfit', 0))
                pct   = float(p.get('percentage', 0))
                lev   = p.get('leverage', '?')
                mark  = float(p.get('markPrice', 0))
                lines.append(
                    f"<b>{p['symbol']}</b> {side} {abs(amt)} @{lev}x\n"
                    f"  入场:{entry:.4f}  标记:{mark:.4f}\n"
                    f"  强平:{liq:.4f}  浮盈:{upnl:+.2f}U ({pct:+.1f}%)"
                )
            await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"❌ 查询失败: {e}")

    @admin_only
    async def cmd_q_balances(update, ctx):
        import sys; sys.path.insert(0, '/root/maomao')
        from trader.exchange import get_all_balances
        try:
            b = get_all_balances()
            lines = ["<b>账户余额</b>\n"]
            lines.append(f"<b>合约</b>")
            lines.append(f"  余额: {b['futures']:.2f} U")
            lines.append(f"  可用: {b['futures_avail']:.2f} U")
            lines.append(f"  浮盈: {b['futures_upnl']:+.2f} U")
            spot = {k: v for k, v in b.get('spot', {}).items() if v >= 1}
            if spot:
                lines.append(f"\n<b>现货</b>")
                for asset, amt in sorted(spot.items(), key=lambda x: -x[1]):
                    lines.append(f"  {asset}: {amt:.4f}")
            funding = {k: v for k, v in b.get('funding', {}).items() if v >= 1}
            if funding:
                lines.append(f"\n<b>资金</b>")
                for asset, amt in sorted(funding.items(), key=lambda x: -x[1]):
                    lines.append(f"  {asset}: {amt:.4f}")
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"❌ 查询失败: {e}")

    async def _do_transfer(update, ctx, transfer_type, desc):
        import sys; sys.path.insert(0, '/root/maomao')
        from trader.exchange import transfer_funds
        if not ctx.args:
            await update.message.reply_text(f"用法: 发送 {desc} 后跟金额，例如：/3 100")
            return
        try:
            amount = float(ctx.args[0])
        except ValueError:
            await update.message.reply_text("❌ 金额格式错误，请输入数字")
            return
        try:
            tran_id = transfer_funds(amount, transfer_type)
            await update.message.reply_text(f"✅ {desc} {amount} USDT\ntranId: {tran_id}")
        except Exception as e:
            await update.message.reply_text(f"❌ 划转失败: {e}")

    @admin_only
    async def cmd_transfer_3(update, ctx):
        await _do_transfer(update, ctx, "MAIN_UMFUTURE", "现货→合约")

    @admin_only
    async def cmd_transfer_4(update, ctx):
        await _do_transfer(update, ctx, "UMFUTURE_MAIN", "合约→现货")

    @admin_only
    async def cmd_transfer_5(update, ctx):
        await _do_transfer(update, ctx, "MAIN_FUNDING", "现货→资金")

    @admin_only
    async def cmd_transfer_6(update, ctx):
        await _do_transfer(update, ctx, "FUNDING_MAIN", "资金→现货")

    @admin_only
    async def cmd_trade_log(update, ctx):
        import sys; sys.path.insert(0, '/root/maomao')
        from trader.trade_log import get_recent, format_for_tg
        n = 20
        if ctx.args:
            try: n = min(50, int(ctx.args[0]))
            except: pass
        entries = get_recent(n)
        text = format_for_tg(entries)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def post_init(app):
        base_cmds = [
            BotCommand("start","状态"), BotCommand("help","帮助"),
            BotCommand("cc","切换API/订阅"), BotCommand("model","切换模型"),
            BotCommand("mode","查看状态"), BotCommand("status","三Bot服务状态"),
            BotCommand("ping","心跳"), BotCommand("log","查看日志"),
            BotCommand("restart","重启本Bot"), BotCommand("stop","关闭本Bot"),
        ]
        if bot_dir == "maomao":
            base_cmds += [
                BotCommand("1","查询所有持仓"),
                BotCommand("2","查询各账户余额"),
                BotCommand("3","现货→合约 /3 <金额>"),
                BotCommand("4","合约→现货 /4 <金额>"),
                BotCommand("5","现货→资金 /5 <金额>"),
                BotCommand("6","资金→现货 /6 <金额>"),
                BotCommand("7","交易日志 /7 [条数]"),
            ]
        await app.bot.set_my_commands(base_cmds)
        state = load_state(bot_dir)
        logger.info(f"{bot_name} v4.2 | {mode_label(state['mode'])} | {state['model']}")
        try:
            await app.bot.send_message(chat_id=admin_id,
                text=f"✅ <b>{bot_name} 上线</b>\n\n模式: {mode_label(state['mode'])}\n模型: <code>{state['model']}</code>",
                parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"上线通知失败: {e}")

    logger.info(f"=== {bot_name} v4.2 启动 ===")
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
    if bot_dir == "maomao":
        app.add_handler(CommandHandler("1", cmd_q_positions))
        app.add_handler(CommandHandler("2", cmd_q_balances))
        app.add_handler(CommandHandler("3", cmd_transfer_3))
        app.add_handler(CommandHandler("4", cmd_transfer_4))
        app.add_handler(CommandHandler("5", cmd_transfer_5))
        app.add_handler(CommandHandler("6", cmd_transfer_6))
        app.add_handler(CommandHandler("7", cmd_trade_log))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_photo))
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)
    async def _heartbeat(ctx):
        logger.info(f"[heartbeat] {bot_name} alive")
    app.job_queue.run_repeating(_heartbeat, interval=300, first=10)

    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        import httpx
        try:
            httpx.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": admin_id, "text": f"🔴 <b>{bot_name} 下线</b>", "parse_mode": "HTML"}, timeout=5)
        except Exception: pass


# ── Claude Code完整Agent模式（大猫+玄玄共用，有记忆/有工具/有会话）──
async def claudecode_gen(prompt, add_dir="/root", bot_dir="damao"):
    async def _gen(step_info):
        import json as _json
        TOOL_ICONS = {
            "Bash": "🖥️", "Read": "📖", "Write": "✍️", "Edit": "✏️",
            "Grep": "🔍", "Glob": "📂", "WebSearch": "🌐", "WebFetch": "🌐",
            "Task": "🤖", "TodoWrite": "📋", "Skill": "⚡",
        }
        step_info["text"] = "启动中"
        step_info["actions"] = []
        session_file = Path(f"/root/{bot_dir}/data/session.json")
        session_id = None
        try:
            session_id = _json.loads(session_file.read_text()).get("session_id")
        except Exception:
            pass
        cmd = ["claude", "--print", "--verbose",
               "--output-format", "stream-json",
               "--permission-mode", "acceptEdits",
               "--add-dir", add_dir]
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.extend(["-p", prompt])
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=add_dir,
            env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"})
        final_result = ""
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            try:
                obj = _json.loads(line)
            except Exception:
                continue
            typ = obj.get("type")
            if typ == "assistant":
                for item in obj.get("message", {}).get("content", []):
                    if item.get("type") == "tool_use":
                        tool_name = item.get("name", "")
                        icon = TOOL_ICONS.get(tool_name, "⚙️")
                        inp = item.get("input", {})
                        brief = str(inp.get("command") or inp.get("file_path") or
                                    inp.get("pattern") or inp.get("query") or
                                    inp.get("prompt") or "")[:50]
                        step_info["actions"].append(f"{icon} {brief or tool_name}")
                        step_info["text"] = f"{icon} {tool_name}"
            elif typ == "result":
                new_sid = obj.get("session_id")
                if new_sid:
                    session_file.parent.mkdir(parents=True, exist_ok=True)
                    session_file.write_text(_json.dumps({"session_id": new_sid}))
                final_result = obj.get("result", "").strip()
        _, stderr_bytes = await proc.communicate()
        step_info["text"] = "完成"
        yield final_result or stderr_bytes.decode("utf-8")[:300] or "（无输出）"
    return _gen
