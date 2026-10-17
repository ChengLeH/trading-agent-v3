"""Whole-response JSON syntax normalization, never prose/semantic extraction."""
import json


def normalize_json_response(text):
    if not isinstance(text, str):
        raise ValueError('response must be JSON text')
    try:
        json.loads(text)
        return text
    except ValueError:
        pass
    lines = text.strip().splitlines()
    if (len(lines) < 3 or lines[0].strip().casefold() not in ('```json', '```')
            or lines[-1].strip() != '```'):
        raise ValueError('response must be one complete JSON value, optionally in a single JSON code fence')
    body = '\n'.join(lines[1:-1])
    json.loads(body)  # reject fragments, explanations and multiple JSON values
    return body
