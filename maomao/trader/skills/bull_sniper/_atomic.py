"""bull_sniper 状态文件原子写小工具

所有 JSON 状态落盘走 atomic_write_json：先写 .tmp，os.replace 原子替换。
断电 / kill -9 / 磁盘满时不会残留半截文件。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(
    path: Path | str,
    data: Any,
    *,
    ensure_ascii: bool = False,
    indent: int | None = 2,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=ensure_ascii, indent=indent))
    os.replace(tmp, path)
