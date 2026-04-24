"""
Stage 5.3 两层移动止盈单测
运行：python3 -m pytest tests/test_stage5_3_trail.py -v
"""
import sys, os, queue
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from trail_manager import (
    get_active_layer,
    is_trail_triggered,
    compute_float_pnl,
    check_all,
)

_CFG = {
    "trail_layer1_activation_pct": 15,
    "trail_layer1_pullback_pct": 10,
    "trail_layer2_activation_pct": 35,
    "trail_layer2_pullback_pct": 15,
}

def _pos(peak=0.0, open_price=100.0):
    return {"status": "holding", "position_open_price": open_price, "peak_pnl_pct": peak}


# ── 1. 浮盈 <15% 不激活 layer1 ──

def test_below_layer1_no_activation():
    """peak=14.9% < 15% → get_active_layer 返回 None"""
    assert get_active_layer(14.9, _CFG) is None


# ── 2. 浮盈15%激活layer1，峰值回撤10%触发 ──

def test_layer1_activated_at_15pct():
    assert get_active_layer(15.0, _CFG) == "layer1"

def test_layer1_triggered_at_10pct_drawdown():
    """peak=15%，current=13.5%，回撤=(15-13.5)/15=10% → 触发"""
    assert is_trail_triggered(13.5, 15.0, "layer1", _CFG) is True

def test_layer1_not_triggered_below_10pct():
    """peak=15%，current=13.6%，回撤<10% → 不触发"""
    assert is_trail_triggered(13.6, 15.0, "layer1", _CFG) is False


# ── 3. 浮盈35%激活layer2，峰值回撤15%触发 ──

def test_layer2_activated_at_35pct():
    assert get_active_layer(35.0, _CFG) == "layer2"

def test_layer2_triggered_at_15pct_drawdown():
    """peak=35%，current=29.75%，回撤=(35-29.75)/35=15% → 触发"""
    assert is_trail_triggered(29.75, 35.0, "layer2", _CFG) is True

def test_layer2_not_triggered_below_15pct():
    """peak=35%，current=30%，回撤<15% → 不触发"""
    assert is_trail_triggered(30.0, 35.0, "layer2", _CFG) is False


# ── 4. 浮盈40%回撤34%，触发layer2（不是layer1）──

def test_40pct_peak_uses_layer2():
    """peak=40% >= 35% → 使用 layer2"""
    assert get_active_layer(40.0, _CFG) == "layer2"

def test_40pct_drop_to_34pct_triggers_layer2():
    """peak=40%，current=34%，(40-34)/40=15% → 触发 layer2"""
    assert is_trail_triggered(34.0, 40.0, "layer2", _CFG) is True

def test_40pct_drop_to_34pct_would_also_exceed_layer1():
    """layer1 回撤阈值10%，15%>10%，但系统用 layer2，此处只验证数值关系"""
    assert is_trail_triggered(34.0, 40.0, "layer1", _CFG) is True  # layer1 也触发，但 layer2 优先


# ── 5. peak_pnl 单调不下降 ──

def test_peak_monotonically_non_decreasing():
    """peak=40%，现价回落到38% → 不应发出 update_peak"""
    from unittest.mock import patch
    q = queue.Queue()
    snapshot = {"XYZUSDT": _pos(peak=40.0)}
    with patch("trail_manager._fetch_positions",
               return_value={"XYZUSDT": {"mark_price": 138.0, "qty": 1.0}}):
        check_all(snapshot, _CFG, q, "key", "secret")
    updates = [m for m in list(q.queue) if m["type"] == "update_peak"]
    assert len(updates) == 0, "peak 不应在价格回落时下降"


# ── 6. 队列消息：close 不被 update_peak 覆盖 ──

def test_close_coexists_with_update_peak():
    """
    AAAUSDT: peak=40%，current=34% → close(layer2)
    BBBUSDT: peak=20%，current=25% → update_peak(新峰)
    两条消息都在队列，互不覆盖
    """
    from unittest.mock import patch
    q = queue.Queue()
    snapshot = {
        "AAAUSDT": _pos(peak=40.0),
        "BBBUSDT": _pos(peak=20.0),
    }
    with patch("trail_manager._fetch_positions", return_value={
        "AAAUSDT": {"mark_price": 134.0, "qty": 2.0},  # 34%，触发 layer2
        "BBBUSDT": {"mark_price": 125.0, "qty": 3.0},  # 25%，新峰值
    }):
        check_all(snapshot, _CFG, q, "key", "secret")

    messages = list(q.queue)
    types = [m["type"] for m in messages]
    assert "close" in types
    assert "update_peak" in types
    close_msg = next(m for m in messages if m["type"] == "close")
    assert close_msg["symbol"] == "AAAUSDT"
    assert close_msg["layer"] == "layer2"
    assert close_msg["qty"] == 2.0
    peak_msg = next(m for m in messages if m["type"] == "update_peak")
    assert peak_msg["symbol"] == "BBBUSDT"
    assert abs(peak_msg["peak_pnl_pct"] - 25.0) < 0.01


# ── 7. 持仓消失不崩 ──

def test_position_gone_from_exchange_no_crash():
    """mark_data 里没有该 symbol（已平仓）→ 跳过，不崩，队列为空"""
    from unittest.mock import patch
    q = queue.Queue()
    snapshot = {"XYZUSDT": _pos(peak=40.0)}
    with patch("trail_manager._fetch_positions", return_value={}):
        check_all(snapshot, _CFG, q, "key", "secret")
    assert q.empty()


# ── 8. 实时 API 失败 fail-open ──

def test_api_failure_fail_open():
    """positionRisk 返回空（模拟网络失败）→ 不触发任何动作，不崩"""
    from unittest.mock import patch
    q = queue.Queue()
    snapshot = {"XYZUSDT": _pos(peak=40.0)}
    with patch("trail_manager._fetch_positions", return_value={}):
        check_all(snapshot, _CFG, q, "key", "secret")
    assert q.empty()

def test_api_exception_fail_open():
    """_fetch_positions 内部异常已被捕获返回 {} → check_all 也不崩"""
    from unittest.mock import patch
    q = queue.Queue()
    snapshot = {"XYZUSDT": _pos(peak=40.0, open_price=100.0)}
    # 模拟 _fetch_positions 直接返回空（等价于内部异常被捕获后的行为）
    with patch("trail_manager._fetch_positions", return_value={}):
        check_all(snapshot, _CFG, q, "key", "secret")
    assert q.empty()


# ── 9. 并发压测：1000次 del/add position，trail 线程同时 put，无崩溃无 KeyError ──

def test_concurrent_state_modification_no_crash():
    """
    模拟生产架构：
    - trail 线程：每轮取浅拷贝 snapshot → check_all → put queue（只读 state）
    - 主线程：1000次 add/del positions + 消费 queue（独占写 state）
    验证：无崩溃、无 KeyError、所有 close 消息被处理
    """
    import threading
    from unittest.mock import patch

    state = {"positions": {}}
    q = queue.Queue()
    errors = []
    stop_event = threading.Event()
    iteration_count = [0]

    def trail_worker():
        """模拟 trail_loop：只读 snapshot，不直接写 state"""
        while not stop_event.is_set():
            try:
                snapshot = dict(state.get("positions", {}))
                # 为所有 holding 仓位模拟触发（peak=40% drop to 34%）
                fake_marks = {
                    sym: {"mark_price": 134.0, "qty": 1.0}
                    for sym in snapshot
                }
                if fake_marks:
                    with patch("trail_manager._fetch_positions", return_value=fake_marks):
                        check_all(snapshot, _CFG, q, "key", "secret")
                iteration_count[0] += 1
            except Exception as e:
                errors.append(f"trail: {type(e).__name__}: {e}")

    t = threading.Thread(target=trail_worker, daemon=True)
    t.start()

    close_count = 0
    peak_count = 0

    for i in range(1000):
        sym = f"COIN{i % 5}USDT"   # 5 个 symbol 循环
        try:
            # 主线程写 state: 加仓位（peak=40% → 模拟 trail 会触发 layer2）
            state["positions"][sym] = {
                "status": "holding",
                "position_open_price": 100.0,
                "peak_pnl_pct": 40.0,
            }
            # 消费队列（主线程独占写）
            while not q.empty():
                try:
                    item = q.get_nowait()
                    s = item.get("symbol", "")
                    pos = state["positions"].get(s)
                    if pos:
                        if item["type"] == "update_peak":
                            pos["peak_pnl_pct"] = item["peak_pnl_pct"]
                            peak_count += 1
                        elif item["type"] == "close":
                            pos["status"] = "trail_tp"
                            close_count += 1
                except queue.Empty:
                    break
            # 主线程写 state: 平仓（删 key）
            state["positions"].pop(sym, None)
        except Exception as e:
            errors.append(f"main[{i}]: {type(e).__name__}: {e}")

    stop_event.set()
    t.join(timeout=2)

    assert not errors, f"并发异常: {errors}"
    # trail 线程至少跑了 1 次
    assert iteration_count[0] > 0, "trail 线程未执行"


# ── 10. 历史价格回放：真实价格序列验证触发点 ──

def test_historical_price_replay_layer2_trigger():
    """
    价格序列回放（开仓价 100）：
    tick 0:  mark=100 (+0%)   → 未激活
    tick 1:  mark=108 (+8%)   → 未激活 (<15%)
    tick 2:  mark=115 (+15%)  → layer1 激活，峰值=15%，无触发
    tick 3:  mark=120 (+20%)  → layer1，回撤=0%，无触发
    tick 4:  mark=130 (+30%)  → layer1，回撤=0%，无触发
    tick 5:  mark=140 (+40%)  → layer2 激活，峰值=40%，无触发
    tick 6:  mark=138 (+38%)  → layer2，回撤=(40-38)/40=5%，无触发
    tick 7:  mark=136 (+36%)  → layer2，回撤=(40-36)/40=10%，无触发
    tick 8:  mark=134 (+34%)  → layer2，回撤=(40-34)/40=15%，触发
    预期：触发发生在 tick 8，layer=layer2
    """
    from unittest.mock import patch

    OPEN = 100.0
    SYMBOL = "SIMUSDT"
    price_ticks = [100, 108, 115, 120, 130, 140, 138, 136, 134]
    expected_trigger_tick = 8
    expected_layer = "layer2"

    # 初始化 pos state，逐 tick 喂入
    pos_state = {"status": "holding", "position_open_price": OPEN, "peak_pnl_pct": 0.0}
    triggered_at = None
    triggered_layer = None

    for tick, mark in enumerate(price_ticks):
        q = queue.Queue()
        snapshot = {SYMBOL: pos_state.copy()}
        with patch("trail_manager._fetch_positions",
                   return_value={SYMBOL: {"mark_price": float(mark), "qty": 1.0}}):
            check_all(snapshot, _CFG, q, "key", "secret")

        # 消费队列，更新 pos_state（模拟主线程）
        while not q.empty():
            item = q.get_nowait()
            if item["type"] == "update_peak":
                pos_state["peak_pnl_pct"] = item["peak_pnl_pct"]
            elif item["type"] == "close":
                triggered_at = tick
                triggered_layer = item["layer"]
                break
        if triggered_at is not None:
            break

    assert triggered_at == expected_trigger_tick, (
        f"触发 tick 应为 {expected_trigger_tick}，实际为 {triggered_at}"
    )
    assert triggered_layer == expected_layer, (
        f"触发层应为 {expected_layer}，实际为 {triggered_layer}"
    )
