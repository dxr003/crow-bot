"""多账户交易路由层 v1.0

从 accounts.yaml 读取账户清单，提供：
- 账户注册中心（registry）
- 合约路由（futures）
- 现货路由（spot）
- 划转（transfer）

运行时启停拔插：改 accounts.yaml 的 enabled 字段，自动生效不重启。
"""
