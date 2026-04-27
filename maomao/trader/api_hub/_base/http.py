# api_hub/_base/http.py
# 统一requests session：重试/timeout/异常转换

import time
import requests
from .errors import ApiError, RateLimitError, AuthError, ServerError, NetworkError

DEFAULT_TIMEOUT = 10
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 1.5  # 每次重试等待倍数

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s

_session = _make_session()

def get(url: str, params=None, headers=None, timeout=DEFAULT_TIMEOUT) -> dict:
    return _request("GET", url, params=params, headers=headers, timeout=timeout)

def post(url: str, data=None, json=None, headers=None, timeout=DEFAULT_TIMEOUT) -> dict:
    return _request("POST", url, data=data, json=json, headers=headers, timeout=timeout)

def _request(method: str, url: str, timeout=DEFAULT_TIMEOUT, **kwargs) -> dict:
    last_err = None
    for attempt in range(DEFAULT_RETRIES):
        try:
            resp = _session.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code == 429:
                raise RateLimitError("触发限速", status_code=429, raw=resp.text)
            if resp.status_code in (401, 403):
                raise AuthError("鉴权失败", status_code=resp.status_code, raw=resp.text)
            if resp.status_code >= 500:
                raise ServerError(f"服务端错误 {resp.status_code}", status_code=resp.status_code, raw=resp.text)
            resp.raise_for_status()
            return resp.json()
        except (RateLimitError, AuthError):
            raise  # 这两类不重试
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = NetworkError(f"网络错误: {e}")
        except ServerError as e:
            last_err = e
        except Exception as e:
            raise ApiError(f"未知错误: {e}")
        if attempt < DEFAULT_RETRIES - 1:
            time.sleep(DEFAULT_BACKOFF ** attempt)
    raise last_err
