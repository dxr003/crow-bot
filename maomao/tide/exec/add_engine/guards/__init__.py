"""守门员包。加新 guard：本目录新建 <kind>.py 用 @register，然后下面 import 一次。"""
from .base import Guard, register, build, list_kinds


def _load_all():
    from . import cooldown      # noqa: F401
    from . import liq_safety    # noqa: F401
    from . import quota         # noqa: F401


_load_all()

__all__ = ["Guard", "register", "build", "list_kinds"]
