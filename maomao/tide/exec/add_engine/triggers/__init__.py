"""触发器包。新加一个触发器：在本目录新建 <kind>.py，用 @register 装饰类，
然后在本文件的 _load_all 里 import 一下即可（或者用 pkgutil 自动扫描）。
"""
from .base import Trigger, register, build, list_kinds


def _load_all():
    # 后续新增的触发器都在这里 import 一次即可被 register
    from . import demo          # noqa: F401
    from . import box_edge      # noqa: F401
    from . import price         # noqa: F401
    from . import tide_reentry  # noqa: F401


_load_all()

__all__ = ["Trigger", "register", "build", "list_kinds"]
