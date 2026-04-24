"""python -m tide.exec.add_engine 的入口，避免和 engine.py 的 __main__ 块重入。"""
import json
import logging
from .engine import run_once

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    r = run_once()
    print(json.dumps(r, ensure_ascii=False, indent=2))
