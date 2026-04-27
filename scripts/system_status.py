#!/usr/bin/env python3
"""系统状态全景 — 验证所有策略真实关闭/开启状态

用法:
  python3 /root/scripts/system_status.py            # 全景
  python3 /root/scripts/system_status.py 幻影        # 单查策略
  python3 /root/scripts/system_status.py 潮汐
  python3 /root/scripts/system_status.py 链上007
  python3 /root/scripts/system_status.py 币安1       # 单查账户
  python3 /root/scripts/system_status.py 币安4
  python3 /root/scripts/system_status.py bn4         # 别名也行
  python3 /root/scripts/system_status.py 持仓        # 4 账户聚合

支持的别名:
  幻影 / bull / bull_sniper / 阻击 / phantom
  潮汐 / tide
  链上007 / 007 / onchain
  币安1/2/3/4 / bn1-4 / main / test / lhb / zgl

防 YAML 陷阱设计：直接 yaml.safe_load 看 Python 端实际类型，不只看配置文件文本。
"""
from __future__ import annotations
import sys
import time
import yaml
import subprocess
import re
sys.path.insert(0, '/root/maomao')

LINE = '━' * 50

def section(title: str) -> None:
    print(f'\n{LINE}\n  {title}\n{LINE}')

def yaml_field(path: str, key_path: list[str], must_be_str: bool = True):
    """读 yaml 字段。返回 (value, type_name, warn_str)
    must_be_str=True 时若实际不是字符串就报警（防 YAML 陷阱：off/on/yes/no → bool）"""
    try:
        cfg = yaml.safe_load(open(path))
        v = cfg
        for k in key_path:
            if not isinstance(v, dict):
                return None, '?', '⚠️ KEY_MISSING'
            v = v.get(k, '__MISSING__')
            if v == '__MISSING__':
                return None, '?', '⚠️ KEY_MISSING'
        t = type(v).__name__
        if must_be_str and not isinstance(v, str):
            return v, t, f'⚠️ YAML 陷阱！应为字符串实际是 {t}'
        return v, t, '✅'
    except Exception as e:
        return None, '?', f'❌ {e}'

def svc(name: str) -> tuple[str, str]:
    try:
        a = subprocess.run(['systemctl', 'is-active', name],
                           capture_output=True, text=True, timeout=3).stdout.strip()
        s = subprocess.run(['systemctl', 'show', name, '-p', 'ActiveEnterTimestamp', '--value'],
                           capture_output=True, text=True, timeout=3).stdout.strip()
        return a, s
    except Exception as e:
        return f'err:{e}', ''

# ── 路由：解析参数 ──
ARG = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ''
ALIAS = {
    'phantom': 'phantom', '幻影': 'phantom', 'bull': 'phantom',
    'bull_sniper': 'phantom', '阻击': 'phantom', '做多阻击': 'phantom',
    'tide': 'tide', '潮汐': 'tide',
    'onchain': 'onchain', '007': 'onchain', '链上007': 'onchain', '链上': 'onchain',
    '币安1': 'bn1', 'bn1': 'bn1', 'main': 'bn1', '玄玄': 'bn1',
    '币安2': 'bn2', 'bn2': 'bn2', 'test': 'bn2', '震天响': 'bn2',
    '币安3': 'bn3', 'bn3': 'bn3', 'lhb': 'bn3', '李红兵': 'bn3',
    '币安4': 'bn4', 'bn4': 'bn4', 'zgl': 'bn4', '专攻组六': 'bn4',
    '持仓': 'positions', '账户': 'positions', '余额': 'balance',
}
target = ALIAS.get(ARG, '' if ARG == '' else 'unknown')

def section_phantom():
    section('【幻影 bull_sniper】')
    phantom_cfg_path = '/root/maomao/trader/skills/bull_sniper/config.yaml'
    v, t, w = yaml_field(phantom_cfg_path, ['bull_sniper', 'mode'])
    is_off = (v == 'off')
    print(f'  全局 mode: {v!r} ({t}) {w}')
    print(f'  → {"⛔ 已完全关闭（不下单）" if is_off else "🟢 自动执行中" if v == "auto" else "🟡 仅推卡片不下单（alert）" if v == "alert" else "❓ 未知状态"}')
    try:
        cfg = yaml.safe_load(open(phantom_cfg_path))
        accs = cfg['bull_sniper'].get('accounts', {})
        for acc, ac in accs.items():
            en = ac.get('enabled', False)
            flag = '🟢 开' if en else '⚪ 关'
            print(f'  {flag} {acc}.enabled: {en}')
    except Exception as e:
        print(f'  ❌ 读账户配置失败: {e}')
    a, s = svc('bull-sniper')
    print(f'  service: {a}  启动时间: {s}')
    return is_off

def section_tide():
    section('【潮汐 tide】')
    tide_path = '/root/maomao/tide/config.yaml'
    v, t, w = yaml_field(tide_path, ['system', 'mode'])
    is_shadow = (v == 'shadow')
    print(f'  system.mode: {v!r} ({t}) {w}')
    print(f'  → {"⛔ 影子盘（不下单）" if is_shadow else "🟢 实盘执行" if v == "live" else "❓ 未知"}')
    v2, t2, w2 = yaml_field(tide_path, ['mock_short_enabled'], must_be_str=False)
    print(f'  mock_short_enabled: {v2!r} ({t2}) {w2}')
    print(f'  → {"⛔ 不接管 bn2 BTC 底仓（手动操盘）" if v2 is False else "🟢 自动管 bn2 BTC 底仓" if v2 is True else "❓ 未知"}')
    a, s = svc('tide')
    print(f'  service: {a}  启动时间: {s}')

def section_onchain():
    section('【链上 007 onchain_007】')
    v, t, w = yaml_field('/root/maomao/trader/skills/onchain_007/config.yaml',
                         ['onchain_007', 'enabled'], must_be_str=False)
    print(f'  enabled: {v!r} ({t}) {w}')
    print(f'  → {"🟢 开启（每小时 :07/:37 推群）" if v is True else "⛔ 关闭" if v is False else "❓ 未知"}')

def section_account(acc_name: str):
    section(f'【{acc_name} 账户实仓】')
    try:
        from trader.multi import executor
        pos = executor.get_positions('玄玄', acc_name)
        ords = executor.get_open_orders('玄玄', acc_name)
        bal = executor.get_balance('玄玄', acc_name)
        f = bal.get('futures', {})
        print(f'  💰 合约: 总额 {f.get("total",0):.2f}U  可用 {f.get("available",0):.2f}U  浮盈 {f.get("upnl",0):+.2f}U')
        if not pos:
            print(f'  📭 0 持仓 / {len(ords)} 挂单')
        else:
            for p in pos:
                amt = float(p['positionAmt']); side = '多' if amt > 0 else '空'
                upnl = float(p['unRealizedProfit'])
                liq = p.get('liquidationPrice', '?')
                print(f"  📊 {p['symbol']} {side} {abs(amt):.4f} 入场 {float(p['entryPrice']):.4f} 浮盈 {upnl:+.2f}U  强平 {liq}")
                # 关联挂单
                for o in [o for o in ords if o['symbol'] == p['symbol']]:
                    tag = '止损' if o['type'] == 'STOP_MARKET' else ('止盈' if o['type'] == 'TAKE_PROFIT_MARKET' else o['type'])
                    print(f"      └ {tag} 触发{o.get('stopPrice') or o.get('price')} id={o['id']}")
        # 孤立挂单
        sym_set = {p['symbol'] for p in pos}
        for o in [o for o in ords if o['symbol'] not in sym_set]:
            print(f'  🔸 孤立挂单 {o["symbol"]} {o["type"]} 触发{o.get("stopPrice") or o.get("price")} id={o["id"]}')
    except Exception as e:
        print(f'  ❌ {e}')

def section_all_positions():
    section('【4 账户实仓汇总】')
    try:
        from trader.multi import executor
        for ACC in ['币安1', '币安2', '币安3', '币安4']:
            try:
                pos = executor.get_positions('玄玄', ACC)
                ords = executor.get_open_orders('玄玄', ACC)
                if not pos:
                    print(f'  [{ACC}] 0 持仓 / {len(ords)} 挂单')
                    continue
                for p in pos:
                    amt = float(p['positionAmt']); side = '多' if amt > 0 else '空'
                    upnl = float(p['unRealizedProfit'])
                    print(f"  [{ACC}] {p['symbol']} {side} {abs(amt):.4f} 入场 {float(p['entryPrice']):.4f} 浮盈 {upnl:+.2f}U")
            except Exception as e:
                print(f'  [{ACC}] ❌ {e}')
    except Exception as e:
        print(f'  ❌ executor 加载失败: {e}')

def section_audit(phantom_mode):
    section('【24h 行为审计】')
    try:
        log = '/root/maomao/trader/skills/bull_sniper/logs/scanner.log'
        cnt_open = cnt_skip = 0
        cutoff = time.time() - 86400
        with open(log, encoding='utf-8') as f:
            for line in f:
                m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                if not m:
                    continue
                ts = time.mktime(time.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'))
                if ts < cutoff:
                    continue
                if '[买入]' in line and 'executed' in line:
                    cnt_open += 1
                elif 'mode=off' in line and '纯记录' in line:
                    cnt_skip += 1
        if phantom_mode == 'off':
            flag = '✅' if cnt_open == 0 else '⚠️ 不一致（含历史 YAML bug 期间数据）'
            print(f'  幻影 24h 自动开仓数: {cnt_open}  {flag}')
        else:
            print(f'  幻影 24h 自动开仓数: {cnt_open}')
        print(f'  幻影 24h mode=off skip 次数: {cnt_skip}')
    except FileNotFoundError:
        print('  scanner.log 不存在')
    except Exception as e:
        print(f'  ❌ 审计失败: {e}')

# ── 路由分发 ──
header_time = f'\n🔍 状态查询  ({time.strftime("%Y-%m-%d %H:%M:%S")})'

if target == 'phantom':
    print(header_time)
    section_phantom()
    print(LINE)
    sys.exit(0)
elif target == 'tide':
    print(header_time)
    section_tide()
    print(LINE)
    sys.exit(0)
elif target == 'onchain':
    print(header_time)
    section_onchain()
    print(LINE)
    sys.exit(0)
elif target in ('bn1', 'bn2', 'bn3', 'bn4'):
    print(header_time)
    section_account({'bn1': '币安1', 'bn2': '币安2', 'bn3': '币安3', 'bn4': '币安4'}[target])
    print(LINE)
    sys.exit(0)
elif target == 'positions':
    print(header_time)
    section_all_positions()
    print(LINE)
    sys.exit(0)
elif target == 'unknown':
    print(f'\n❌ 未知参数: {ARG!r}')
    print(f'   支持: 幻影/潮汐/链上007/币安1-4/持仓 等，或不带参数看全景')
    sys.exit(1)

# ── 全景模式（默认）──
print(f'\n🔍 系统状态全景  ({time.strftime("%Y-%m-%d %H:%M:%S")})')
phantom_mode_val = None

# ── 1. 幻影 ──
section('【幻影 bull_sniper】')
phantom_cfg_path = '/root/maomao/trader/skills/bull_sniper/config.yaml'
v, t, w = yaml_field(phantom_cfg_path, ['bull_sniper', 'mode'])
print(f'  全局 mode: {v!r} ({t}) {w}')
phantom_mode = v if isinstance(v, str) else None
try:
    cfg = yaml.safe_load(open(phantom_cfg_path))
    accs = cfg['bull_sniper'].get('accounts', {})
    for acc, ac in accs.items():
        en = ac.get('enabled', False)
        flag = '🟢 开' if en else '⚪ 关'
        print(f'  {flag} {acc}.enabled: {en}')
except Exception as e:
    print(f'  ❌ 读账户配置失败: {e}')
a, s = svc('bull-sniper')
print(f'  service: {a}  启动时间: {s}')

# ── 2. 潮汐 ──
section('【潮汐 tide】')
tide_path = '/root/maomao/tide/config.yaml'
v, t, w = yaml_field(tide_path, ['system', 'mode'])
print(f'  system.mode: {v!r} ({t}) {w}')
v2, t2, w2 = yaml_field(tide_path, ['mock_short_enabled'], must_be_str=False)
print(f'  mock_short_enabled: {v2!r} ({t2}) {w2}')
a, s = svc('tide')
print(f'  service: {a}  启动时间: {s}')

# ── 3. 链上 007 ──
section('【链上 007 onchain_007】')
v, t, w = yaml_field('/root/maomao/trader/skills/onchain_007/config.yaml',
                     ['onchain_007', 'enabled'], must_be_str=False)
print(f'  enabled: {v!r} ({t}) {w}')

# ── 4. 4 账户实仓 ──
section('【4 账户实仓】')
try:
    from trader.multi import executor
    for ACC in ['币安1', '币安2', '币安3', '币安4']:
        try:
            pos = executor.get_positions('玄玄', ACC)
            ords = executor.get_open_orders('玄玄', ACC)
            if not pos:
                print(f'  [{ACC}] 0 持仓 / {len(ords)} 挂单')
                continue
            for p in pos:
                amt = float(p['positionAmt'])
                side = '多' if amt > 0 else '空'
                upnl = float(p['unRealizedProfit'])
                print(f"  [{ACC}] {p['symbol']} {side} {abs(amt):.4f} 入场 {float(p['entryPrice']):.4f} 浮盈 {upnl:+.2f}U")
        except Exception as e:
            print(f'  [{ACC}] ❌ {e}')
except Exception as e:
    print(f'  ❌ executor 加载失败: {e}')

# ── 5. 24h 行为审计（验证 mode 一致性）──
section('【24h 行为审计】')
try:
    log = '/root/maomao/trader/skills/bull_sniper/logs/scanner.log'
    cnt_open = cnt_skip = 0
    cutoff = time.time() - 86400
    with open(log, encoding='utf-8') as f:
        for line in f:
            m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
            if not m:
                continue
            ts = time.mktime(time.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'))
            if ts < cutoff:
                continue
            if '[买入]' in line and 'executed' in line:
                cnt_open += 1
            elif 'mode=off' in line and '纯记录' in line:
                cnt_skip += 1
    if phantom_mode == 'off':
        flag = '✅' if cnt_open == 0 else '⚠️ 不一致！mode=off 但有自动开仓'
        print(f'  幻影 24h 自动开仓数: {cnt_open}  {flag}')
    elif phantom_mode == 'alert':
        flag = '✅' if cnt_open == 0 else '⚠️ alert 模式不该开仓'
        print(f'  幻影 24h 自动开仓数: {cnt_open}  {flag}')
    else:  # auto 或 None / bug
        if phantom_mode is None:
            print(f'  ⚠️ phantom_mode 不是合法字符串（YAML 陷阱），24h 开仓: {cnt_open}')
        else:
            print(f'  幻影 24h 自动开仓数: {cnt_open}（auto 模式正常）')
    print(f'  幻影 24h mode=off skip 次数: {cnt_skip}')
except FileNotFoundError:
    print('  scanner.log 不存在')
except Exception as e:
    print(f'  ❌ 审计失败: {e}')

# ── 6. 一致性总结 ──
section('【一致性总结】')
issues = []
v, t, _ = yaml_field(phantom_cfg_path, ['bull_sniper', 'mode'])
if not isinstance(v, str):
    issues.append('幻影 mode 不是字符串（YAML 陷阱）')
v, t, _ = yaml_field(tide_path, ['system', 'mode'])
if not isinstance(v, str):
    issues.append('潮汐 system.mode 不是字符串（YAML 陷阱）')

if not issues:
    print('  ✅ 所有 mode 字段类型正确，无 YAML 陷阱')
else:
    for i in issues:
        print(f'  ⚠️ {i}')

print(f'\n{LINE}\n')
