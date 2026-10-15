"""Structured errors. Codes are the reason codes of the contracts (ARCHITECTURE §7)."""

from __future__ import annotations

from typing import Any


class AetherError(Exception):
    """An expected, explainable failure with a stable machine-readable code.

    ``http_status`` maps it onto the API; ``retryable`` drives the job retry policy (ADR-017):
    only transient infrastructure failures are retryable, never contract violations.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 422,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class InvariantViolation(AetherError):
    """A scientific invariant was violated. This is a defect, never a user error."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, http_status=500, retryable=False, details=details)


class NotFound(AetherError):
    def __init__(self, what: str, ident: str) -> None:
        super().__init__("not_found", f"{what} {ident} not found", http_status=404)
