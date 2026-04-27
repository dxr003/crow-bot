"""链上 007 热榜 cron 入口（每小时 07/37 跑两次）

用法：
  python3 runner.py            # 拉数据 + 推群（生产）
  python3 runner.py --dry      # 拉数据 + 渲染卡片到 stdout，不推群
  python3 runner.py --no-filter # 不过滤，直接推（调试用）
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv("/root/maomao/.env")

if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# 2026-04-27 Step 4-E: shared.control_loader 必须在其他业务 import 之前，避免被 /root/shared 抢占
if "/root/maomao" not in sys.path:
    sys.path.insert(0, "/root/maomao")
from shared.control_loader import get_007_enabled  # noqa: E402

from fetcher import fetch_trending  # noqa: E402
from filter_layer import calc_stars, pass_filter  # noqa: E402
from notifier import next_seq, push_to_group, record_push, render_card  # noqa: E402

(BASE / "logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(BASE / "logs" / "runner.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("onchain_007")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="不推 TG，只打印")
    ap.add_argument("--no-filter", action="store_true", help="跳过过滤，全部展示")
    args = ap.parse_args()

    cfg = yaml.safe_load((BASE / "config.yaml").read_text())["onchain_007"]
    # 2026-04-27 Step 4-E: enabled 走 control.yaml（防 YAML 陷阱 + emergency_stop 接入）
    if not get_007_enabled() and not args.dry:
        log.info("disabled (control.yaml), skip")
        return

    import time as _t
    quota = cfg["network_quota"]
    selected: list[dict] = []
    for idx, (network, n) in enumerate(quota.items()):
        if idx > 0:
            _t.sleep(2.5)  # 链间隔 2.5s 避开 GeckoTerminal 30/min 限速
        try:
            pools = fetch_trending(network, limit=20)
        except Exception as e:
            log.warning(f"[{network}] fetch fail: {e}")
            continue

        if args.no_filter:
            passed = pools
        else:
            passed = []
            for p in pools:
                ok, why = pass_filter(p, cfg)
                if ok:
                    passed.append(p)
                else:
                    log.debug(f"[{network}] filter {p['symbol']}: {why}")
        log.info(f"[{network}] {len(passed)}/{len(pools)} passed filter")
        for p in passed[:n]:
            p["stars"] = calc_stars(p, cfg)
            selected.append(p)

    log.info(f"total selected: {len(selected)}")

    seq = next_seq()
    text = render_card(selected, cfg, seq)

    if args.dry:
        print(text)
        return

    ok = push_to_group(text, cfg)
    record_push(seq, len(selected), ok)
    if ok:
        log.info(f"pushed seq=#{seq} count={len(selected)}")
    else:
        log.error(f"push failed seq=#{seq}")


if __name__ == "__main__":
    main()
