"""Known CLI terminal diagnostics and whole-answer recovery, never Claim semantics."""
from .response_json import normalize_json_response


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
