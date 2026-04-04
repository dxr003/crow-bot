#!/usr/bin/env python3
"""毛毛 v4.1 — 薄壳（逻辑全在 shared/core.py）"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.core import create_and_run_bot

if __name__ == "__main__":
    create_and_run_bot(
        env_path=str(Path(__file__).parent / ".env"),
    )
