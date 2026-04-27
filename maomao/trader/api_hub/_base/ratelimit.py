# api_hub/_base/ratelimit.py
# 简单限速装饰器——按调用间隔限速

import time
import functools

def rate_limit(calls_per_minute: int):
    """装饰器：限制调用频率"""
    min_interval = 60.0 / calls_per_minute
    last_called = {}

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            key = func.__qualname__
            now = time.time()
            elapsed = now - last_called.get(key, 0)
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            last_called[key] = time.time()
            return func(*args, **kwargs)
        return wrapper
    return decorator
