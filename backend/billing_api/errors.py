from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, issues: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.issues = issues or []

    def to_body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "issues": self.issues}}


def not_found(what: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", f"{what} not found")
