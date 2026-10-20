"""Known CLI terminal diagnostics and whole-answer recovery, never Claim semantics."""
from .response_json import normalize_json_response


_QUOTA_CODES = {"quota_exceeded", "insufficient_quota", "quota_reached", "usage_limit_exceeded"}
_RATE_CODES = {"rate_limit_exceeded", "rate_limited", "too_many_requests", "resource_exhausted"}
PROVIDER_ERROR_CODES = frozenset(_QUOTA_CODES | _RATE_CODES)


def safe_provider_reference(value):
    return (isinstance(value, str) and 1 <= len(value) <= 128 and value.isascii()
            and any(c.isalnum() for c in value) and all(c.isalnum() or c in "._:-" for c in value))


def classify_provider_failure(result):
    """Classify known provider codes first, then one documented CLI diagnostic.

    Returns (error code, safe metadata), or None for unknown failures. Arbitrary
    provider prose is never promoted to a quota decision or copied to metadata.
    """
    if not isinstance(result, dict):
        return None
    if result.get("status") not in (None, "ERROR"):
        return None
    raw_code = result.get("error_code")
    code = raw_code.casefold().strip() if isinstance(raw_code, str) else ""
    failure = "quota_exhausted" if code in _QUOTA_CODES else "rate_limited" if code in _RATE_CODES else None
    diagnostic = result.get("error")
    if not isinstance(diagnostic, str):
        diagnostic = result.get("message")
    if not isinstance(diagnostic, str):
        diagnostic = ""
    if failure is None and diagnostic.casefold().strip() == "individual quota reached":
        failure = "quota_exhausted"
    if failure is None:
        return None
    metadata = {}
    if code and (code in _QUOTA_CODES or code in _RATE_CODES):
        metadata["provider_error_code"] = code
    if diagnostic.casefold().strip() == "individual quota reached":
        metadata["provider_diagnostic"] = "Individual quota reached"
    reference = result.get("event_id", result.get("request_id"))
    if isinstance(reference, str) and safe_provider_reference(reference):
        metadata["event_reference"] = reference
    return failure, metadata


def is_stream_interruption(result):
    """Recognize one CLI diagnostic, never inspect Claim or response prose."""
    return (result.get("status") == "ERROR" and result.get("error_code") is None
            and result.get("error") == "The stream was interrupted. Please continue the task you were working on.")


def interrupted_json_response(result):
    """Recover only a whole JSON answer carried by the known interrupted result."""
    if not is_stream_interruption(result):
        return None
    try:
        return normalize_json_response(result.get("response"))
    except (ValueError, TypeError):
        return None
