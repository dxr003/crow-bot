# BUG: E 因子对部分币 NoneType 崩溃

**发现时间**：2026-04-19 18:35
**影响币**：ARKMUSDT（可能还有其他非 Alpha 币）
**严重度**：中（该币 E 段永远拿不到分，六维评分少一条腿）

## 现象

scanner.log 反复报：
```
[WARNING] [E因子] ARKMUSDT 跳过: int() argument must be a string, a bytes-like object or a real number, not 'NoneType'
```

从 17:53 到 18:24 累计 15+ 次。BOMEUSDT 同期 E 因子正常跑。
区别：BOMEUSDT 是币安 Alpha 币（有合约地址 0x85e1a551），ARKMUSDT 不是。

## 根因推测

`chain_score.py` 里某段 `int(value)` 直接转换，但 value 是从 API 拿回来的字段，非 Alpha 币对应字段返回 `None`，`int(None)` 直接炸。

## 修法（爸爸定调）

**不能为了简化就弱化 E 因子**——爸爸说：
> E 因子现在是币安 Alpha 有才有，后期会提供付费 API，要保持正常，万一搜索到就有用。

所以修法是**防护而非降级**：
- 所有从 API 拿回来的字段加 None 检查
- `int(None)` → 默认值 0（或其他合理默认）
- 该子项拿 0 分，但整段 E 不能崩
- 有数据的币仍按完整逻辑评分

## 参考位置
- 报错栈可以在 scanner.log 搜 "[E因子] ARKMUSDT"
- 代码：`/root/maomao/trader/skills/bull_sniper/chain_score.py`
- 爸爸要求保持 E 因子评分能力完整，别为了绕开 bug 就砍功能
