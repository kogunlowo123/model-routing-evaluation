"""Domain errors mapped to HTTP status codes and OAuth-style error bodies by the API layer."""

from __future__ import annotations

from typing import Any


class RoutingError(Exception):
    code = "invalid_request"
    status = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(RoutingError):
    code = "not_found"
    status = 404


class ConflictError(RoutingError):
    code = "conflict"
    status = 409


class UnauthorizedError(RoutingError):
    code = "unauthorized"
    status = 401


class ForbiddenError(RoutingError):
    code = "forbidden"
    status = 403


class ApprovalRequiredError(RoutingError):
    """Policy requires a human decision; the action was recorded but not executed."""

    code = "approval_required"
    status = 403


class NoEligibleProviderError(RoutingError):
    """Compliance, pool or cost constraints left no provider able to serve the request."""

    code = "no_eligible_provider"
    status = 422


class UnprocessableError(RoutingError):
    code = "unprocessable"
    status = 422


class OutputBlockedError(RoutingError):
    """The model answered but the output failed a content control and was withheld."""

    code = "output_blocked"
    status = 502


class UpstreamUnavailableError(RoutingError):
    """A dependency (model providers, identity fabric) could not be reached."""

    code = "upstream_unavailable"
    status = 503
