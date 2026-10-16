"""Gemini API client for external fact verification (Phase 6 verifier).

Uses gemini-3.8-flash as an independent model family from the main
pipeline (Muse), so verification errors are uncorrelated.

Two backends (in priority order):
1. `agy` CLI (preferred): uses the user's Google subscription quota via
   OAuth login. Reliable, no 503s. Requires `agy` installed and logged in.
   Model: gemini-3.8-flash-high
2. Direct API (fallback): uses $GEMINI_API_KEY or ~/.config/gemini/api_key.
   Free tier, may 503 during high demand.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.request
import urllib.error

MODEL = "gemini-3.8-flash"
AGY_MODEL = "gemini-3.8-flash-high"
_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _agy_available() -> bool:
    # 檢查 ~/.local/bin/agy 和 PATH
    local_agy = os.path.expanduser("~/.local/bin/agy")
    if os.path.exists(local_agy) and os.access(local_agy, os.X_OK):
        return True
    return shutil.which("agy") is not None


def _generate_via_agy(prompt: str, timeout: int = 3600) -> str:
    """Call agy CLI in print mode. Timeout 1hr for large prompts."""
    result = subprocess.run(
        ["agy", "--model", AGY_MODEL,
         "--dangerously-skip-permissions",
         "-p", prompt],
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "PATH": f"{os.path.expanduser('~/.local/bin')}:{os.environ.get('PATH', '')}"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"agy failed: {result.stderr[:300]}")
    return result.stdout.strip()


def get_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    cfg = os.path.expanduser("~/.config/gemini/api_key")
    if os.path.exists(cfg):
        with open(cfg) as f:
            return f.read().strip()
    raise RuntimeError(
        "Gemini API key not found: set $GEMINI_API_KEY or "
        "write it to ~/.config/gemini/api_key"
    )


def generate(prompt: str, model: str = MODEL, max_output_tokens: int = 8192,
             temperature: float = 0.2, retries: int = 3) -> str:
    """Call Gemini generateContent, return the text response.

    Tries agy CLI first (subscription quota, reliable), falls back to
    direct API (free tier) if agy is unavailable.
    """
    # 優先用 agy（訂閱配額，穩定）
    if _agy_available():
        try:
            return _generate_via_agy(prompt)
        except Exception as e:
            # agy 失敗才 fallback 到 API
            pass
    return _generate_via_api(prompt, model, max_output_tokens,
                             temperature, retries)


def _generate_via_api(prompt: str, model: str = MODEL,
                      max_output_tokens: int = 8192,
                      temperature: float = 0.2, retries: int = 3) -> str:
    """Direct Gemini API call (fallback)."""
    key = get_api_key()
    url = f"{_API_BASE}/models/{model}:generateContent?key={key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_output_tokens,
            "temperature": temperature,
        },
    }).encode()

    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:500]
            # 503/429: transient, retry with backoff
            if e.code in (429, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt * 5)
                last_err = f"HTTP {e.code}: {body}"
                continue
            raise RuntimeError(f"Gemini API HTTP {e.code}: {body}")
        except Exception as e:
            last_err = str(e)
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 5)
                continue
            raise RuntimeError(f"Gemini API failed: {last_err}")
    raise RuntimeError(f"Gemini API failed after {retries} retries: {last_err}")


def verify_facts(prompt: str) -> list:
    """Run the fact verifier prompt, parse JSON array from response."""
    text = generate(prompt, max_output_tokens=16384)
    # 抽 JSON array（容忍前後雜訊）
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0:
        raise RuntimeError(f"verifier did not return JSON array: {text[:300]}")
    return json.loads(text[start:end + 1])
