class SyncError(Exception):
    """可安全显示给用户的错误；不包含响应正文或认证 URL。"""


class AuthExpired(SyncError):
    pass


class PermissionDenied(SyncError):
    pass


class NetworkError(SyncError):
    pass


class InvalidResponse(SyncError):
    pass
