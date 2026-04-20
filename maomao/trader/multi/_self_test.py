"""executor.py 自检（不真下单，只测权限+查询）"""
from trader.multi.executor import (
    get_balance, get_positions, get_open_orders,
    get_all_balances, open_market, close_market, place_stop_loss,
)

print("═══ 权限拦截测试 ═══")
# 天天不能碰币安1
try:
    get_balance("天天", "币安1")
    print("  ❌ 天天查币安1 应该被拒但通过了")
except PermissionError as e:
    print(f"  ✅ 天天查币安1 → 拒绝: {e}")

# 天天不能下单币安1
try:
    open_market("天天", "币安1", "BTCUSDT", "BUY", 10, 5)
    print("  ❌ 天天开仓币安1 应该被拒但通过了")
except PermissionError as e:
    print(f"  ✅ 天天开仓币安1 → 拒绝: {e}")

# 路人没权限
try:
    get_balance("路人", "币安2")
    print("  ❌ 路人 应该被拒")
except PermissionError as e:
    print(f"  ✅ 路人查币安2 → 拒绝")

print("\n═══ 玄玄全账户余额 ═══")
balances = get_all_balances("玄玄")
for name, info in balances.items():
    if "error" in info:
        print(f"  ⚠️ {name}: {info['error']}")
        continue
    fut = info["futures"]
    usdt_spot = info["spot"].get("USDT", 0)
    print(f"  {name}: 合约 {fut['total']:.2f}U / 可用 {fut['available']:.2f}U / 现货USDT {usdt_spot:.2f}")

print("\n═══ 天天全账户余额（应看不到币安1）═══")
tt_balances = get_all_balances("天天")
print(f"  天天能看: {list(tt_balances.keys())}")
assert "币安1" not in tt_balances, "天天不该看到币安1"
print("  ✅ 币安1 被正确屏蔽")

print("\n═══ 玄玄查单账户余额（别名测试）═══")
r1 = get_balance("玄玄", "main")       # 别名 = 币安1
r2 = get_balance("玄玄", "李红兵")      # 别名 = 币安3
print(f"  main(→币安1): 合约 {r1['futures']['total']:.2f}U")
print(f"  李红兵(→币安3): 合约 {r2['futures']['total']:.2f}U")

print("\n✅ 自检全部通过（未实际下单）")
