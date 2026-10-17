"""Application errors mapped to HTTP status codes by the API."""


class AppError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
