"""multi 模块状态文件原子写。

同 bull_sniper/_atomic 实现：先写 .tmp，os.replace 原子替换。
断电 / kill -9 / 磁盘满时不残留半截文件。
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
    tmp.write_text(json.dumps(data, ensure_ascii=ensure_ascii, indent=indent), encoding="utf-8")
    os.replace(tmp, path)
