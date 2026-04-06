# 实时数据查询规则

## 铁律：实时数据必须调 API，不得靠记忆推断

凡是涉及余额/持仓/挂单/浮盈/强平价，必须先执行命令查询再回复。
不许猜，不许用对话里出现过的数字，不许推测"大概是"。

---

## 查询命令

### 查余额
```bash
python3 -c "
from trader.exchange import get_client
c = get_client()
info = c.futures_account()
bal = float(info['totalWalletBalance'])
avail = float(info['availableBalance'])
upnl = float(info['totalUnrealizedProfit'])
print(f'余额: {bal:.2f}U  可用: {avail:.2f}U  浮盈: {upnl:.2f}U')
"
```

### 查持仓
```bash
python3 -c "
from trader.exchange import get_client
c = get_client()
pos = [p for p in c.futures_position_information() if float(p['positionAmt']) != 0]
if not pos:
    print('当前无持仓')
for p in pos:
    amt = float(p['positionAmt'])
    side = '多' if amt > 0 else '空'
    entry = float(p['entryPrice'])
    upnl = float(p['unRealizedProfit'])
    lev = p['leverage']
    print(f"{p['symbol']} {side} {abs(amt):.4f}  入场:{entry:.4f}  {lev}x  浮盈:{upnl:.2f}U")
"
```

### 查挂单
```bash
python3 -c "
from trader.exchange import get_client
c = get_client()
orders = c.futures_get_open_orders()
if not orders:
    print('当前无挂单')
for o in orders:
    price = o.get('stopPrice') or o.get('price')
    print(f"{o['symbol']} {o['side']} {o['type']} 价格:{price} 数量:{o['origQty']}")
"
```

---

## 触发场景（以下任一情况必须先查 API）

- 爸爸问余额/钱/账户/资金
- 爸爸问仓位/持仓/有没有仓/开了什么
- 爸爸问挂单/止损/止盈挂没挂上
- 爸爸问浮盈/浮亏/盈亏
- 爸爸问强平价/爆仓价
- 爸爸说"查一下""看看""是多少"

---

## 禁止

- 根据对话内容推断当前余额/持仓数字
- 用记忆里的数据当实时数据
- 说"刚才你说开了SOL……"然后直接报数字
- 推测"大概是""应该还有"

实时数据只有 API 返回值才算数。
