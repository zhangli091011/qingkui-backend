"""Small strict-JSON model client for offline governance workflows."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from app.config import settings


@dataclass(frozen=True)
class JsonModelResult:
    value: dict[str, Any]
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


def _parse_object(content: str) -> dict[str, Any]:
    content = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
    if fenced:
        content = fenced.group(1)
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("Model output must be one JSON object")
    return value


def request_json(
    *,
    system_prompt: str,
    data: dict[str, Any],
    max_tokens: int,
    stub_factory: Callable[[dict[str, Any]], dict[str, Any]],
) -> JsonModelResult:
    if settings.ai_provider == "stub":
        return JsonModelResult(stub_factory(data), "stub", "governance-json-stub")
    if not settings.deepseek_api_key:
        raise RuntimeError("DeepSeek API key is not configured")
    untrusted = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"<untrusted_data>{untrusted}</untrusted_data>"},
        ],
        "temperature": 0,
        "max_tokens": min(max_tokens, settings.deepseek_max_tokens),
        "response_format": {"type": "json_object"},
        # v4-flash may otherwise spend the entire budget in reasoning_content
        # and return an empty final content field.
        "thinking": {"type": "disabled"},
    }
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=settings.deepseek_timeout_seconds) as client:
                response = client.post(
                    f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                    json=payload,
                )
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2**attempt)
                continue
            response.raise_for_status()
            body = response.json()
            usage = body.get("usage") or {}
            return JsonModelResult(
                _parse_object(body["choices"][0]["message"]["content"]),
                "deepseek",
                body.get("model", settings.deepseek_model),
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
            )
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    detail = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown error"
    raise RuntimeError(f"Strict JSON model request failed: {detail}") from last_error
