#!/usr/bin/env python3
# ============================================================
# setup_keys.py — 交互式Key导入
# 运行: python3 setup_keys.py
# 按提示输入，自动写入三个.env文件
# ============================================================
from pathlib import Path

def ask(label: str, default: str = "") -> str:
    if default:
        val = input(f"  {label}\n  [已有值，直接回车保留，或输入新值]: ").strip()
        return val if val else default
    else:
        while True:
            val = input(f"  {label}: ").strip()
            if val:
                return val
            print("  ⚠️  不能为空，请重新输入")

def ask_optional(label: str, default: str = "") -> str:
    hint = f"[已有: {default[:6]}...，回车保留]" if default else "[回车跳过]"
    val = input(f"  {label} {hint}: ").strip()
    return val if val else default

print("""
╔════════════════════════════════════════╗
║     三Bot Key 导入工具                 ║
║     自动写入 /root/damao/maomao/baobao ║
╚════════════════════════════════════════╝
""")

# ── 公共 ──────────────────────────────
print("━━━ 公共配置 ━━━━━━━━━━━━━━━━━━━━━━━━")
ADMIN_ID = ask("你的 Telegram User ID", "509640925")
ANTHROPIC = ask_optional("Anthropic API Key（新建后填）")
OPENROUTER = ask_optional("OpenRouter API Key（新建后填）")

# ── 大猫 ──────────────────────────────
print("\n━━━ 大猫 Bot ━━━━━━━━━━━━━━━━━━━━━━━━")
DAMAO_TOKEN = ask("大猫 Bot Token", "8506007563:AAFWFB-EmlS9wD3EUOo_ROH68tkX6q0t9hs")

# ── 毛毛 ──────────────────────────────
print("\n━━━ 毛毛 Bot（交易） ━━━━━━━━━━━━━━━━━")
MAOMAO_TOKEN = ask("毛毛 Bot Token", "8799101926:AAF-2D4T2tPAdXwTwxY_l8z0Q7qulJoeDrA")
BN_KEY    = ask_optional("Binance API Key（新建后填）")
BN_SECRET = ask_optional("Binance Secret Key（新建后填）")
HL_API    = ask("HL API Addr",     "0x4587a30647e9b11d4122b3d1ba9ca6ec7ae9b912")
HL_PRIV   = ask("HL Private Key",  "0xcc9517146a3d11802bd172f05df9d006947cab76ce712497b4b3ed606bf19a86")
HL_ACCT   = ask("HL Account Addr", "0x8fdD22BaeB49ECD5556C04f8Be2Cd4237d1eA203")
CG_KEY    = ask("Coinglass API Key", "5a8071e22ce741b2bb17c107077eb3c6")

# ── 播报 ──────────────────────────────
print("\n━━━ 播报 Bot ━━━━━━━━━━━━━━━━━━━━━━━━")
BAOBAO_TOKEN = ask("播报 Bot Token", "8743597962:AAFHWAw1GMJZ5Y9N7hxI4sixjgElYbTKVMQ")
BC_CHAT   = ask_optional("播报目标频道/群 ID（负数，可稍后填）")

# ── 写文件 ────────────────────────────
print("\n━━━ 写入文件 ━━━━━━━━━━━━━━━━━━━━━━━━")

damao_env = f"""BOT_TOKEN={DAMAO_TOKEN}
ADMIN_ID={ADMIN_ID}
ANTHROPIC_API_KEY={ANTHROPIC}
"""

maomao_env = f"""BOT_TOKEN={MAOMAO_TOKEN}
ADMIN_ID={ADMIN_ID}
AI_ENABLED=false
ANTHROPIC_API_KEY={ANTHROPIC}
OPENROUTER_API_KEY={OPENROUTER}
BINANCE_API_KEY={BN_KEY}
BINANCE_SECRET_KEY={BN_SECRET}
HL_API_ADDR={HL_API}
HL_PRIVATE_KEY={HL_PRIV}
HL_ACCOUNT_ADDR={HL_ACCT}
HL_TESTNET=0
COINGLASS_API_KEY={CG_KEY}
"""

baobao_env = f"""BOT_TOKEN={BAOBAO_TOKEN}
ADMIN_ID={ADMIN_ID}
BROADCAST_CHAT_ID={BC_CHAT}
"""

paths = {
    "/root/damao/.env":  damao_env,
    "/root/maomao/.env": maomao_env,
    "/root/baobao/.env": baobao_env,
}

for path, content in paths.items():
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    print(f"  ✅ {path}")

print("""
╔════════════════════════════════════════╗
║  全部写入完成！                        ║
║  下一步: bash 01_deploy.sh             ║
╚════════════════════════════════════════╝
""")
