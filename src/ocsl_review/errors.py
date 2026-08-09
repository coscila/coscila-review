"""Stable user-facing exception types and process exit codes."""

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_TRUST = 3
EXIT_POLICY = 4
EXIT_VALIDATION = 5


class ReviewError(RuntimeError):
    """An expected failure that should be printed without a traceback."""


class TrustBoundaryError(ReviewError):
    """An operator action was attempted from an untrusted workspace."""


class ConfidentialityError(ReviewError):
    """A proposed external request violates the review egress policy."""


class ValidationError(ReviewError):
    """An artifact or catalog violates a declared contract."""


class ProviderError(ReviewError):
    """A bounded scholarly-provider operation failed."""
