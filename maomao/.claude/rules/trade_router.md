# 交易指令路由规则

收到用户消息时，第一步永远是：运行 python3 -c "from trader.router import try_trade_command; r = try_trade_command('用户消息内容'); print(r)" 

判断结果：
- 如果输出不是 None → 直接把输出回复给用户，不要加任何额外解释
- 如果输出是 None → 按正常AI对话处理

支持的指令格式举例：
- 做多 SOL 5x 20u 止损 65（复合指令，开仓+挂止损）
- 开多 BTC 10x 100 限价 85000
- 止盈 BTC 90000
- 平 ETH / 平多 ETH
- 做多 btc 5（最简：市价全仓10x 5U）

注意：用户消息中的单引号要转义。如果router返回了结果就直接回复，不要二次加工。
