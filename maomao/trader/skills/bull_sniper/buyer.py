#!/usr/bin/env python3
"""
bull_sniper buyer.py — 买入执行模块（占位）

当前阶段：纯记录，不实际下单
后续对接底座执行链：parser → order → exchange
"""
import logging

logger = logging.getLogger("bull_buyer")


def execute(symbol: str, price: float, analyze_result: dict, cfg: dict) -> dict:
    """
    执行买入（当前阶段只记录，不下单）

    参数:
      symbol: 交易对 (e.g. XYZUSDT)
      price: 当前价格
      analyze_result: analyzer返回的完整结果
      cfg: bull_sniper配置

    返回:
      {"status": "skipped"/"executed"/"error", "reason": str, "order_id": str|None}
    """
    mode = cfg.get("mode", "off")

    if mode == "off":
        logger.info(f"[buyer] {symbol} mode=off, 纯记录不下单")
        return {"status": "skipped", "reason": "mode=off纯记录阶段", "order_id": None}

    if mode == "alert":
        logger.info(f"[buyer] {symbol} mode=alert, 推送等待人工确认")
        return {"status": "skipped", "reason": "mode=alert等待人工确认", "order_id": None}

    # mode == "auto" — 后续对接底座执行链
    logger.info(f"[buyer] {symbol} mode=auto, 自动下单（待实现）")
    return {"status": "skipped", "reason": "auto模式待实现", "order_id": None}
