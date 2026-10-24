"""Small wrapper around the Anthropic Messages API."""
import json
import re

import httpx

import config


class LLMUnavailable(RuntimeError):
    pass


def ask_json(system: str, prompt: str, max_tokens: int = 800) -> dict:
    """Ask Claude for a JSON object. Raises LLMUnavailable on any failure."""
    if not config.ANTHROPIC_API_KEY:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": config.ANTHROPIC_MODEL,
                "max_tokens": max_tokens,
                "system": system + "\nReply with a single JSON object and nothing else.",
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        match = re.search(r"\{.*\}", text, re.S)
        return json.loads(match.group(0) if match else text)
    except Exception as e:  # network, HTTP, or parse errors
        raise LLMUnavailable(str(e)) from e
