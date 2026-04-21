# reports/

子代理（subagent）产出文件落盘目录。

## 命名规范

`{task_name}_{YYYYMMDD_HHMMSS}.md`

示例：
- `crow_review_multi_20260422_093015.md`
- `nansen_smart_money_sweep_20260422_140000.md`
- `efactor_backtest_v3_20260425_080000.md`

## 文件必须包含四段（摘要契约）

```markdown
## 结论（≤5 行）
## 关键发现（按严重度排序）
## 待乌鸦决策项
## 详细产出（可长）
```

主大猫派出子代理后，只把前三段推送 TG；第四段保留在磁盘，乌鸦要细节时按需取。

## 清理

子代理产出按时间戳归档，超过 30 天的文件可人工清理。目前无自动清理任务。
