# api_hub/_base/auth.py
# 签名算法：币安HMAC-SHA256

import hashlib
import hmac
import time

def binance_sign(params: dict, secret: str) -> dict:
    """给params加timestamp+signature，返回完整params"""
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    sig = hmac.new(
        secret.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    params["signature"] = sig
    return params
