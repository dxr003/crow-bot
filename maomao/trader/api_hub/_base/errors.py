# api_hub/_base/errors.py
# 统一异常类型——业务代码只catch这里的类，不catch requests异常

class ApiError(Exception):
    """所有api_hub异常的基类"""
    def __init__(self, msg, status_code=None, raw=None):
        super().__init__(msg)
        self.status_code = status_code
        self.raw = raw

class RateLimitError(ApiError):
    """触发限速（429）"""

class AuthError(ApiError):
    """签名错误/KEY无效（401/403）"""

class ServerError(ApiError):
    """交易所服务端错误（5xx）"""

class NetworkError(ApiError):
    """网络超时/连接失败"""
