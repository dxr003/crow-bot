"""bull_sniper 前置过滤器（2026-04-21 起）

每个 filter 提供：
    is_tradeable(symbol) -> tuple[bool, str]   # (通过?, 原因)
    get_stats() -> dict                         # 调试/dry-run 用

挂载点（scanner.py 解封后加）：按 config.yaml 的 pre_filters 列表顺序串联，
任一 False 即剔除并写 filter_log.jsonl。
"""
