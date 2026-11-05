"""Stable user-facing exception types and process exit codes."""

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_TRUST = 3
EXIT_POLICY = 4
EXIT_VALIDATION = 5
# A failure that may clear by itself: running the same command again later can succeed (sysexits EX_TEMPFAIL).
EXIT_TRANSIENT = 75


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


class TransientError(ReviewError):
    """A provider or transport failure outlasted the command's own retries; the same command can run again later."""
