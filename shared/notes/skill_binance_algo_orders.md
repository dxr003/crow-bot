# 币安条件单（algoOrder）查询坑

## 坑
bull_sniper 开仓后挂的止损走的是 `/fapi/v1/algoOrder` 端点（条件单/算法单），
**不会出现在 `/fapi/v1/openOrders`（普通挂单列表）里**。

直接查普通挂单会误判"没止损"，把爸爸吓到。

## 正确查询

```python
# 按 algoId 查单个条件单实况
_api_get('/fapi/v1/algoOrder', {'algoId': <algoId>}, key, secret)
# 返回: {algoStatus: NEW, triggerPrice: 'xxx', closePosition: True, ...}
```

bull_sniper 开仓日志里有 algoId，形如：
```
[buyer] [币安2] SKLUSDT 止损 @ 0.00861 algoId:1000001380983331
```

## 完整挂单清单
爸爸问"止损挂没"时，要同时查：
1. `/fapi/v1/openOrders` — 普通挂单（限价单、市价条件单由主动交易下的）
2. `/fapi/v1/algoOrder?algoId=xxx` — bull_sniper/自动交易挂的条件单

缺一不可。
