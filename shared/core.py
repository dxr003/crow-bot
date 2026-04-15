#!/usr/bin/env python3
"""
共享底盘 v4.3 — Claude Code 代理核心
大猫和玄玄共用，永远走 Claude Code 代理模式。
读图走独立 ANTHROPIC_API_KEY，聊天永远走订阅。
# 底座已固定 v4.3 — 新功能只通过 trader/ 模块挂载，禁止修改此文件
"""
import os, json, logging, asyncio, time, base64, io, re, html as html_mod
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode


def _md_to_html(text: str) -> str:
    """Markdown → TG HTML：代码块、行内代码、粗体、斜体"""
    # 多行代码块 ```lang\n...\n``` → <pre><code>...</code></pre>
    def _code_block(m):
        code = html_mod.escape(m.group(2))
        return f"<pre><code>{code}</code></pre>"
    text = re.sub(r"```(\w*)\n(.*?)```", _code_block, text, flags=re.DOTALL)
    # 行内代码 `...` → <code>...</code>
    text = re.sub(r"`([^`]+)`", lambda m: f"<code>{html_mod.escape(m.group(1))}</code>", text)
    # 粗体 **...** → <b>...</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # 斜体 *...* → <i>...</i>
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    return text

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
        if "model" not in s:
            s["model"] = MODELS[0]
        return s
    except Exception:
        return {"model": MODELS[0]}

def save_state(bot_dir, state):
    _state_file(bot_dir).write_text(json.dumps(state, ensure_ascii=False))

def model_short(model):
    return model.replace("claude-", "").replace("-20251001", "")


# ── Claude Code 完整 Agent（聊天永远走这里）──
async def claudecode_gen(prompt, add_dir="/root", bot_dir="damao", model=None):
    async def _gen(step_info):
        import json as _json
        TOOL_ICONS = {
            "Bash": "🖥️", "Read": "📘", "Write": "📒", "Edit": "📙",
            "Grep": "🔍", "Glob": "🗂️", "WebSearch": "🌐", "WebFetch": "🌏",
            "Task": "🦾", "TodoWrite": "📌", "Skill": "✨",
        }
        step_info["text"] = "启动中"
        step_info["actions"] = []
        cmd = ["claude", "--print", "--verbose",
               "--output-format", "stream-json",
               "--permission-mode", "auto",
               "--continue",
               "--add-dir", add_dir]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(["-p", prompt])
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=add_dir,
            env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"})
        final_result = ""
        async def _read():
            nonlocal final_result
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
                    final_result = obj.get("result", "").strip()
        try:
            await asyncio.wait_for(_read(), timeout=600)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            step_info["text"] = "超时"
            yield "⏱️ Claude响应超时（600s），已终止进程"
            return
        _, stderr_bytes = await proc.communicate()
        step_info["text"] = "完成"
        yield final_result or stderr_bytes.decode("utf-8")[:300] or "（无输出）"
    return _gen


# ── API 读图（仅用于图片，不用于聊天）──
async def api_gen_image(prompt, api_key, model, system_prompt, image_b64):
    async def _gen(step_info):
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
            {"type": "text", "text": prompt or "请描述这张图片"}
        ]
        step_info["text"] = "读图中"
        loop = asyncio.get_event_loop()
        queue = asyncio.Queue()
        def _stream():
            try:
                with client.messages.stream(model=model, max_tokens=4096, system=system_prompt,
                    messages=[{"role": "user", "content": content}]) as stream:
                    step_info["text"] = "生成描述"
                    for text in stream.text_stream:
                        loop.call_soon_threadsafe(queue.put_nowait, text)
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, f"\n⚠️ 读图失败: {e}")
                loop.call_soon_threadsafe(queue.put_nowait, None)
        loop.run_in_executor(None, _stream)
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk
    return _gen


# ── 计时器 UI ──
async def ask_with_timer(update, gen_coro, model):
    thinking_msg = await update.message.reply_text(f"🐦 乌鸦团队 · 启动中... 0s")
    start_time = time.time()
    step_info = {"text": "启动中"}
    async def ticker():
        while True:
            await asyncio.sleep(1)
            elapsed = int(time.time() - start_time)
            actions = step_info.get("actions")
            if actions:
                lines = "\n".join(f"  {a}" for a in actions[-10:])
                txt = f"⚡ {elapsed}s\n{lines}"
            else:
                txt = f"🐦 乌鸦团队 · {step_info['text']}... {elapsed}s"
            try:
                await thinking_msg.edit_text(txt)
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
    try:
        await thinking_msg.delete()
    except Exception:
        pass
    if not full_text.strip():
        full_text = "（无输出）"
    html_text = _md_to_html(full_text)
    chunks = [html_text[i:i+4000] for i in range(0, len(html_text), 4000)]
    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
        except Exception:
            await update.message.reply_text(chunk)


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
    if bot_dir == "damao":
        _add_dir = claude_add_dir or "/root"
    elif bot_dir == "maomao":
        _add_dir = "/root/maomao"
    elif bot_dir == "tiantian":
        _add_dir = "/root/tiantian"
    else:
        _add_dir = claude_add_dir or "/root"

    def admin_only(func):
        async def wrapper(update, ctx):
            if update.effective_user.id != admin_id:
                return
            return await func(update, ctx)
        return wrapper

    async def ask_claude(update, prompt, image_b64=None):
        from datetime import datetime as _dt
        now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        prompt = f"[当前时间: {now_str}] {prompt}"
        state = load_state(bot_dir)
        model = state.get("model", MODELS[0])
        if image_b64:
            if not anthropic_key:
                await update.message.reply_text("⚠️ ANTHROPIC_API_KEY 未设置，无法读图")
                return
            gen = await api_gen_image(prompt, anthropic_key, model, system_prompt, image_b64)
        else:
            gen = await claudecode_gen(prompt, add_dir=_add_dir, bot_dir=bot_dir, model=model)
        await ask_with_timer(update, gen, model)

    @admin_only
    async def cmd_start(update, ctx):
        state = load_state(bot_dir)
        icon = '🐱' if bot_name == '大猫' else '🐦' if bot_name == '玄玄' else '🐶' if bot_name == '贝贝' else '🐾'
        await update.message.reply_text(
            f"{icon} <b>{bot_name} v4.3</b>\n\n"
            f"模型: <code>{state['model']}</code>\n\n"
            f"/model — 切换模型\n/help — 全部命令",
            parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_help(update, ctx):
        state = load_state(bot_dir)
        if bot_dir == "tiantian":
            await update.message.reply_text(
                f"<b>{bot_name}命令</b>\n\n"
                f"模型: <code>{model_short(state['model'])}</code>\n\n"
                f"/model   — 切换模型\n"
                f"/status  — Bot状态\n"
                f"/restart — 重启\n/stop — 关闭\n\n"
                f"/1 — 查持仓\n/2 — 查余额\n"
                f"/3 — 现货→合约\n/4 — 合约→现货\n"
                f"/5 — 现货→资金\n/6 — 资金→现货\n\n"
                f"<i>发文字 = 问{bot_name}\n发图片 = Vision读图</i>",
                parse_mode=ParseMode.HTML)
            return
        await update.message.reply_text(
            f"<b>{bot_name}命令</b>\n\n"
            f"模型: <code>{model_short(state['model'])}</code>\n\n"
            f"/model  — 循环切换模型\n"
            f"/mode   — 查看状态\n/status — 四Bot服务状态\n"
            f"/ping   — 心跳\n/log    — 查看日志\n\n"
            f"<i>发文字 = 问{bot_name}\n发图片 = Vision读图（消耗API额度）</i>",
            parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_model(update, ctx):
        state = load_state(bot_dir)
        cur = state.get("model", MODELS[0])
        try:
            idx = MODELS.index(cur)
        except ValueError:
            idx = 0
        nxt = MODELS[(idx + 1) % len(MODELS)]
        state["model"] = nxt
        save_state(bot_dir, state)
        await update.message.reply_text(
            f"✅ 模型已切换\n\n`{model_short(cur)}`\n↓\n`{model_short(nxt)}`",
            parse_mode=ParseMode.MARKDOWN)

    @admin_only
    async def cmd_mode(update, ctx):
        if bot_dir == "tiantian":
            state = load_state(bot_dir)
            await update.message.reply_text(
                f"<b>{bot_name} 状态</b>\n\n模型: <code>{state['model']}</code>",
                parse_mode=ParseMode.HTML)
            return
        import shutil
        state = load_state(bot_dir)
        api_ok = "✅" if anthropic_key else "❌"
        cli_ok = "✅" if shutil.which("claude") else "⚠️ 未找到"
        await update.message.reply_text(
            f"<b>{bot_name} 状态</b>\n\n"
            f"模型: <code>{state['model']}</code>\n\n"
            f"📦 Claude Code: {cli_ok}\n"
            f"🖼 读图API: {api_ok}",
            parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_ping(update, ctx):
        state = load_state(bot_dir)
        await update.message.reply_text(
            f"🏓 {bot_name}存活 · `{model_short(state['model'])}`",
            parse_mode=ParseMode.MARKDOWN)

    @admin_only
    async def cmd_status(update, ctx):
        import subprocess as sp
        if bot_dir == "tiantian":
            svcs = ["tiantian"]
        else:
            svcs = ["damao", "maomao", "tiantian", "baobao"]
        lines = []
        for svc in svcs:
            r = sp.run(["systemctl", "is-active", svc], capture_output=True, text=True)
            icon = "✅" if r.stdout.strip() == "active" else "❌"
            lines.append(f"{icon} {svc}: {r.stdout.strip()}")
        await update.message.reply_text(
            "<b>服务状态</b>\n\n" + "\n".join(lines),
            parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_log(update, ctx):
        if bot_dir == "tiantian":
            await update.message.reply_text("⛔ 此命令未开放")
            return
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
        if bot_dir in ("maomao", "tiantian"):
            import sys; sys.path.insert(0, f'/root/{bot_dir}')
            stripped = user_text.strip()
            if stripped in _CONFIRM_WORDS:
                from trader.preview import pop_latest_pending
                order = pop_latest_pending()
                if order is not None:
                    from trader.order import execute
                    try:
                        result = execute(order)
                        try:
                            from trader.trade_log import log_trade
                            log_trade(order=order, result=result)
                        except ImportError:
                            pass
                    except Exception as e:
                        result = f"❌ 执行失败: {e}"
                        try:
                            from trader.trade_log import log_trade
                            log_trade(order=order, error=str(e))
                        except ImportError:
                            pass
                    await update.message.reply_text(result, parse_mode=ParseMode.HTML)
                    return
            elif stripped in _CANCEL_WORDS:
                from trader.preview import pop_latest_pending
                order = pop_latest_pending()
                if order is not None:
                    await update.message.reply_text("❌ 已取消")
                    return
            from trader.router import try_trade_command
            trade_result = try_trade_command(user_text)
            if trade_result is not None:
                result_text, uid = trade_result
                await update.message.reply_text(result_text, parse_mode=ParseMode.HTML)
                if uid is None:
                    try:
                        from trader.trade_log import log_trade
                        log_trade(raw_text=user_text, result=result_text)
                    except ImportError:
                        pass
                if uid is not None:
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
        caption = update.message.caption or "请描述这张图片"
        await ask_claude(update, caption, image_b64)

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
        _bot_log("error", str(ctx.error)[:200])

    @admin_only
    async def cmd_q_positions(update, ctx):
        import sys; sys.path.insert(0, f'/root/{bot_dir}')
        try:
            if bot_dir == "maomao":
                from trader.multi_account import get_all_positions
                positions = get_all_positions()
            else:
                from trader.exchange import get_positions
                positions = [dict(p, _account="") for p in get_positions()]
            if not positions or all("_error" in p for p in positions):
                await update.message.reply_text("📭 当前无持仓")
                return
            # 查挂单（SL/TP状态）
            open_orders = {}
            try:
                if bot_dir == "maomao":
                    from trader.multi_account import ACCOUNTS, _load_client
                    for acct in ACCOUNTS:
                        try:
                            cli, _, _ = _load_client(acct)
                            for o in cli.get_open_orders():
                                open_orders.setdefault(o["symbol"], []).append(o)
                        except Exception:
                            pass
                else:
                    from trader.exchange import get_client
                    for o in get_client().futures_get_open_orders():
                        open_orders.setdefault(o["symbol"], []).append(o)
            except Exception:
                pass
            # 查自研移动止盈状态
            tl_state = {}
            try:
                import json as _json
                from pathlib import Path as _Path
                _tl = _Path("/root/maomao/trader/skills/bull_sniper/data/trailing_limit_state.json")
                if _tl.exists():
                    tl_state = _json.loads(_tl.read_text())
            except Exception:
                pass
            lines = []
            cur_account = None
            for p in positions:
                if "_error" in p:
                    lines.append(f"\n<b>【{p['_account']}】</b> ❌ {p['_error']}")
                    continue
                acct = p.get("_account", "")
                if acct and acct != cur_account:
                    lines.append(f"\n<b>【{acct}】</b>")
                    cur_account = acct
                sym  = p['symbol']
                amt  = float(p['positionAmt'])
                side = "多" if amt > 0 else "空"
                entry = float(p['entryPrice'])
                liq   = float(p.get('liquidationPrice', 0))
                upnl  = float(p.get('unRealizedProfit', 0))
                lev   = p.get('leverage', '?')
                mark  = float(p.get('markPrice', 0))
                margin = float(p.get('isolatedWallet', 0)) or float(p.get('initialMargin', 0))
                notional = abs(amt) * mark
                pct = (upnl / margin * 100) if margin > 0 else 0
                pnl_icon = "🟢" if upnl >= 0 else "🔴"
                liq_str = f"{liq:.4f}" if liq > 0 else "全仓"
                # SL/TP 状态
                sl_tag, tp_tag = "SL❌", "TP❌"
                orders = open_orders.get(sym, [])
                for o in orders:
                    ot = o.get("type", "")
                    sp = o.get("stopPrice", "0")
                    if ot == "STOP_MARKET":
                        sl_tag = f"SL✅ {float(sp):.4f}"
                    elif ot in ("TRAILING_STOP_MARKET", "TAKE_PROFIT_MARKET"):
                        tp_tag = f"TP✅ 原生"
                if sym in tl_state:
                    tp_tag = f"TP✅ 移动止盈"
                lines.append(
                    f"<b>{p['symbol']}</b> {side} {lev}x\n"
                    f"  持仓: {abs(amt):.4f}  保证金: {margin:.2f}U\n"
                    f"  入场: {entry:.4f}  现价: {mark:.4f}\n"
                    f"  强平: {liq_str}  仓位值: {notional:.2f}U\n"
                    f"  {pnl_icon} 浮盈: {upnl:+.2f}U ({pct:+.1f}%)\n"
                    f"  {sl_tag}  {tp_tag}"
                )
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"❌ 查询失败: {e}")

    @admin_only
    async def cmd_q_balances(update, ctx):
        import sys; sys.path.insert(0, f'/root/{bot_dir}')
        try:
            if bot_dir == "maomao":
                from trader.multi_account import get_all_balances as get_multi_bal
                accounts = get_multi_bal()
                lines = ["💰 <b>全账户余额</b>"]
                total_all = 0
                stables = {"USDT", "USDC", "BUSD", "FDUSD"}
                for b in accounts:
                    if "error" in b:
                        lines.append(f"\n<b>【{b['name']}】</b> ❌ {b['error']}")
                        continue
                    upnl = b['futures_upnl']
                    equity = b['futures'] + upnl
                    acct_total = equity
                    upnl_icon = "🟢" if upnl >= 0 else "🔴"
                    lines.append(f"\n<b>【{b['name']}】</b>")
                    lines.append(f"  合约余额: {b['futures']:.2f}U")
                    lines.append(f"  可用保证金: {b['futures_avail']:.2f}U")
                    lines.append(f"  {upnl_icon} 浮盈: {upnl:+.2f}U")
                    lines.append(f"  净值: {equity:.2f}U")
                    spot = {k: v for k, v in b.get('spot', {}).items() if v >= 1}
                    if spot:
                        spot_items = []
                        for asset, amt in sorted(spot.items(), key=lambda x: -x[1]):
                            spot_items.append(f"{asset}:{amt:.4f}")
                            if asset in stables:
                                acct_total += amt
                        lines.append(f"  现货: {', '.join(spot_items)}")
                    funding = {k: v for k, v in b.get('funding', {}).items() if v >= 1}
                    if funding:
                        fund_items = []
                        for asset, amt in sorted(funding.items(), key=lambda x: -x[1]):
                            fund_items.append(f"{asset}:{amt:.4f}")
                            if asset in stables:
                                acct_total += amt
                        lines.append(f"  资金: {', '.join(fund_items)}")
                    lines.append(f"  <b>小计: {acct_total:.2f}U</b>")
                    total_all += acct_total
                lines.append(f"\n━━━━━━━━━━━━━━━━━━━━")
                lines.append(f"<b>全账户合计: {total_all:.2f}U</b>")
            else:
                from trader.exchange import get_all_balances
                b = get_all_balances()
                upnl = b['futures_upnl']
                equity = b['futures'] + upnl
                total = equity
                upnl_icon = "🟢" if upnl >= 0 else "🔴"
                stables = {"USDT", "USDC", "BUSD", "FDUSD"}
                lines = ["💰 <b>账户余额</b>\n"]
                lines.append(f"  合约余额: {b['futures']:.2f}U")
                lines.append(f"  可用保证金: {b['futures_avail']:.2f}U")
                lines.append(f"  {upnl_icon} 浮盈: {upnl:+.2f}U")
                lines.append(f"  净值: {equity:.2f}U")
                spot = {k: v for k, v in b.get('spot', {}).items() if v >= 1}
                if spot:
                    lines.append("\n<b>现货</b>")
                    for asset, amt in sorted(spot.items(), key=lambda x: -x[1]):
                        lines.append(f"  {asset}: {amt:.4f}")
                        if asset in stables:
                            total += amt
                funding = {k: v for k, v in b.get('funding', {}).items() if v >= 1}
                if funding:
                    lines.append("\n<b>资金</b>")
                    for asset, amt in sorted(funding.items(), key=lambda x: -x[1]):
                        lines.append(f"  {asset}: {amt:.4f}")
                        if asset in stables:
                            total += amt
                lines.append(f"\n━━━━━━━━━━━━━━━━━━━━")
                lines.append(f"<b>合计: {total:.2f}U</b>")
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"❌ 查询失败: {e}")

    async def _do_transfer(update, ctx, transfer_type, desc):
        import sys; sys.path.insert(0, f'/root/{bot_dir}')
        from trader.exchange import transfer_funds
        if not ctx.args:
            await update.message.reply_text(f"用法: {desc} 后跟金额，例如：/3 100")
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
        import sys; sys.path.insert(0, f'/root/{bot_dir}')
        from trader.trade_log import get_recent, format_for_tg
        n = 20
        if ctx.args:
            try: n = min(50, int(ctx.args[0]))
            except: pass
        entries = get_recent(n)
        await update.message.reply_text(format_for_tg(entries), parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_bot_log(update, ctx):
        import sys; sys.path.insert(0, f'/root/{bot_dir}')
        from trader.bot_log import get_recent_bot_events, format_bot_events_tg
        n = 20
        if ctx.args:
            try: n = min(50, int(ctx.args[0]))
            except: pass
        entries = get_recent_bot_events(n)
        await update.message.reply_text(format_bot_events_tg(entries), parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_sys_log(update, ctx):
        import sys; sys.path.insert(0, f'/root/{bot_dir}')
        from trader.bot_log import get_recent_sys_snapshots, format_sys_snapshot_tg
        n = 6
        if ctx.args:
            try: n = min(24, int(ctx.args[0]))
            except: pass
        entries = get_recent_sys_snapshots(n)
        await update.message.reply_text(format_sys_snapshot_tg(entries), parse_mode=ParseMode.HTML)

    def _bot_log(event, detail=""):
        if bot_dir in ("maomao", "tiantian"):
            try:
                import sys; sys.path.insert(0, f'/root/{bot_dir}')
                from trader.bot_log import log_bot_event
                log_bot_event(event, detail)
            except Exception:
                pass

    async def post_init(app):
        base_cmds = [
            BotCommand("start","状态"), BotCommand("help","帮助"),
            BotCommand("model","切换模型"),
            BotCommand("mode","查看状态"), BotCommand("status","四Bot服务状态"),
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
                BotCommand("8","Bot运行事件 /8 [条数]"),
                BotCommand("9","系统快照 /9 [条数]"),
            ]
        elif bot_dir == "tiantian":
            base_cmds += [
                BotCommand("1","查询持仓"),
                BotCommand("2","查询余额"),
                BotCommand("3","现货→合约 /3 <金额>"),
                BotCommand("4","合约→现货 /4 <金额>"),
                BotCommand("5","现货→资金 /5 <金额>"),
                BotCommand("6","资金→现货 /6 <金额>"),
            ]
        await app.bot.set_my_commands(base_cmds)
        state = load_state(bot_dir)
        logger.info(f"{bot_name} v4.3 | Claude Code代理 | {state['model']}")
        _bot_log("online", f"Claude Code代理 | {model_short(state['model'])}")
        try:
            await app.bot.send_message(chat_id=admin_id,
                text=f"✅ <b>{bot_name} 上线</b>\n\n模型: <code>{state['model']}</code>",
                parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"上线通知失败: {e}")

    logger.info(f"=== {bot_name} v4.3 启动 ===")
    app = Application.builder().token(bot_token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("stop", cmd_stop_bot))
    if bot_dir in ("maomao", "tiantian"):
        app.add_handler(CommandHandler("1", cmd_q_positions))
        app.add_handler(CommandHandler("2", cmd_q_balances))
        app.add_handler(CommandHandler("3", cmd_transfer_3))
        app.add_handler(CommandHandler("4", cmd_transfer_4))
        app.add_handler(CommandHandler("5", cmd_transfer_5))
        app.add_handler(CommandHandler("6", cmd_transfer_6))
    if bot_dir == "maomao":
        app.add_handler(CommandHandler("7", cmd_trade_log))
        app.add_handler(CommandHandler("8", cmd_bot_log))
        app.add_handler(CommandHandler("9", cmd_sys_log))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_photo))
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)

    async def _heartbeat(ctx):
        logger.info(f"[heartbeat] {bot_name} alive")
        if bot_dir in ("maomao", "tiantian"):
            try:
                import sys; sys.path.insert(0, f'/root/{bot_dir}')
                from trader.bot_log import log_sys_snapshot
                log_sys_snapshot()
            except Exception:
                pass
    app.job_queue.run_repeating(_heartbeat, interval=300, first=10)

    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        _bot_log("offline", "正常退出")
        import httpx
        try:
            httpx.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": admin_id, "text": f"🔴 <b>{bot_name} 下线</b>", "parse_mode": "HTML"}, timeout=5)
        except Exception:
            pass
