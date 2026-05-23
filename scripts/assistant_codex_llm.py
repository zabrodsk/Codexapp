#!/usr/bin/env python3
"""Small Rocky-owned Codex OAuth text helper for assistant skills."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_AUTH_PROFILE_PATHS = [
    Path("/Users/clawdbot/.openclaw/agents/main/agent/auth-profiles.json"),
    Path("/Users/clawdbot/.openclaw/agents/betty-mail-eval/agent/auth-profiles.json"),
]
DEFAULT_MODELS = ["gpt-5.5", "gpt-5.3-mini"]
TASK_LLM_POLICY_VERSION = "rocky-task-llm-v1"
AUTH_PROFILE_KEY = "openai-codex:default"
SENSITIVE_RE = re.compile(
    r"(Bearer\s+[A-Za-z0-9._~+/=-]+|access[_-]?token|refresh[_-]?token|"
    r"cookie|password|secret|credential|auth|https?://[^\s]*(?:token|secret|auth|cookie)[^\s]*)",
    re.IGNORECASE,
)


class AssistantCodexLLMError(RuntimeError):
    """Classified, safe-to-surface LLM helper error."""

    def __init__(self, reason: str, message: str = "", *, attempts: list[dict[str, Any]] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.safe_message = redact_sensitive(message)
        self.attempts = attempts or []
        self.error_hash = hash_text(message or reason)


@dataclass(frozen=True)
class CodexOAuthProfile:
    access: str
    account_id: str
    path: str


def hash_text(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def redact_sensitive(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:500]


def task_llm_models() -> list[str]:
    raw = os.getenv("ROCKY_TASK_LLM_MODELS", "")
    models = [item.strip() for item in raw.split(",") if item.strip()] or list(DEFAULT_MODELS)
    ordered: list[str] = []
    for model in models:
        if model not in ordered:
            ordered.append(model)
    return ordered


def task_llm_auth_profile_paths() -> list[Path]:
    paths: list[Path] = []
    override = os.getenv("ROCKY_TASK_LLM_AUTH_PROFILE")
    if override:
        paths.append(Path(override).expanduser())
    paths.extend(DEFAULT_AUTH_PROFILE_PATHS)
    ordered: list[Path] = []
    for path in paths:
        if path not in ordered:
            ordered.append(path)
    return ordered


def load_codex_oauth_profile(paths: Iterable[Path] | None = None) -> CodexOAuthProfile | None:
    for path in paths or task_llm_auth_profile_paths():
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except Exception:
            continue
        profiles = payload.get("profiles") or {}
        candidates: list[dict[str, Any]] = []
        default_profile = profiles.get(AUTH_PROFILE_KEY) or payload.get(AUTH_PROFILE_KEY)
        if isinstance(default_profile, dict):
            candidates.append(default_profile)
        for key, item in profiles.items():
            if key == AUTH_PROFILE_KEY:
                continue
            if isinstance(item, dict) and str(item.get("provider") or "").lower() == "openai-codex":
                candidates.append(item)
        for profile in candidates:
            access = str(profile.get("access") or profile.get("access_token") or "")
            account_id = str(
                profile.get("account_id")
                or profile.get("accountId")
                or profile.get("chatgpt_account_id")
                or profile.get("chatgptAccountId")
                or ""
            )
            if access and account_id:
                return CodexOAuthProfile(access=access, account_id=account_id, path=str(path))
    return None


def codex_user_agent() -> str:
    return os.getenv("ROCKY_TASK_LLM_USER_AGENT") or f"pi ({platform.system().lower()} {platform.release()}; {platform.machine().lower()})"


def build_codex_request_body(prompt: str, *, model: str, session_id: str) -> dict[str, Any]:
    return {
        "model": model,
        "store": False,
        "stream": True,
        "instructions": "You are Rocky's structured task extraction helper. Return only the requested JSON.",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "text": {"verbosity": "high"},
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": session_id,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {"effort": os.getenv("ROCKY_TASK_LLM_REASONING", "medium"), "summary": "auto"},
    }


def iter_sse_events(response: Any) -> Iterable[dict[str, Any]]:
    event_lines: list[str] = []
    for raw_line in response.iter_lines(decode_unicode=True):
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line or "")
        if not line:
            if event_lines:
                event = _parse_sse_event(event_lines)
                if event is not None:
                    yield event
                event_lines = []
            continue
        event_lines.append(line)
    if event_lines:
        event = _parse_sse_event(event_lines)
        if event is not None:
            yield event


def _parse_sse_event(lines: list[str]) -> dict[str, Any] | None:
    data_parts: list[str] = []
    event_type = ""
    for line in lines:
        if line.startswith("event:"):
            event_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_parts.append(line.split(":", 1)[1].strip())
    if not data_parts:
        return {"type": event_type} if event_type else None
    data = "\n".join(data_parts)
    if data == "[DONE]":
        return {"type": "response.done"}
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return {"type": event_type or "malformed", "raw_hash": hash_text(data)}
    if event_type and isinstance(payload, dict) and "type" not in payload:
        payload["type"] = event_type
    return payload


def extract_output_text(response_payload: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in response_payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text") or content.get("output_text")
            if text:
                texts.append(str(text))
    return "".join(texts).strip()


def generate_codex_text(
    prompt: str,
    *,
    models: list[str] | None = None,
    profile: CodexOAuthProfile | None = None,
    timeout_seconds: int = 60,
    request_post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    profile = profile or load_codex_oauth_profile()
    if profile is None:
        raise AssistantCodexLLMError("task_llm_auth_missing", "No usable OpenAI Codex OAuth profile found.")
    attempts: list[dict[str, Any]] = []
    requests_module = None
    if request_post is None:
        try:
            import requests as requests_module  # type: ignore[no-redef]
        except Exception as exc:
            requests_module = None
    post = request_post or (requests_module.post if requests_module is not None else _urllib_post)
    for model in models or task_llm_models():
        started = time.monotonic()
        attempt = {"model": model, "status": "started"}
        attempts.append(attempt)
        session_id = f"rocky-task-{hash_text(str(started) + model)}"
        headers = {
            "Authorization": f"Bearer {profile.access}",
            "chatgpt-account-id": profile.account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "pi",
            "User-Agent": codex_user_agent(),
            "accept": "text/event-stream",
            "content-type": "application/json",
            "session_id": session_id,
        }
        try:
            response = post(
                CODEX_RESPONSES_URL,
                headers=headers,
                json=build_codex_request_body(prompt, model=model, session_id=session_id),
                stream=True,
                timeout=(30, timeout_seconds),
            )
            if getattr(response, "status_code", 200) >= 300:
                detail = getattr(response, "text", "")
                attempt.update({"status": "failed", "reason": "task_llm_model_failed", "error_hash": hash_text(detail)})
                continue
            delta_text: list[str] = []
            completed: dict[str, Any] | None = None
            for event in iter_sse_events(response):
                event_type = str(event.get("type") or "")
                if event_type in {"error", "response.failed"}:
                    message = event.get("message") or (event.get("response") or {}).get("error", {}).get("message") or event
                    raise AssistantCodexLLMError("task_llm_model_failed", str(message), attempts=attempts)
                if event_type == "response.output_text.delta" and event.get("delta"):
                    delta_text.append(str(event.get("delta")))
                if event_type in {"response.completed", "response.done"}:
                    completed = event.get("response") if isinstance(event.get("response"), dict) else event
            text = extract_output_text(completed or {}) if completed else ""
            if not text and delta_text:
                text = "".join(delta_text).strip()
            if text:
                attempt.update({"status": "ok", "duration_ms": int((time.monotonic() - started) * 1000)})
                return {
                    "status": "ok",
                    "provider": "openai_codex_oauth",
                    "model": model,
                    "text": text,
                    "attempts": sanitize_attempts(attempts),
                    "profile_path": profile.path,
                    "policy_version": TASK_LLM_POLICY_VERSION,
                }
            attempt.update({"status": "failed", "reason": "task_llm_empty_output"})
        except TimeoutError as exc:
            attempt.update({"status": "failed", "reason": "task_llm_timeout", "error_hash": hash_text(str(exc))})
            continue
        except AssistantCodexLLMError:
            raise
        except Exception as exc:
            reason = "task_llm_model_failed"
            attempt.update({"status": "failed", "reason": reason, "error_hash": hash_text(str(exc))})
            continue
    final_reason = next((item.get("reason") for item in reversed(attempts) if item.get("reason")), "task_llm_model_failed")
    raise AssistantCodexLLMError(str(final_reason), "All Codex task LLM attempts failed.", attempts=sanitize_attempts(attempts))


class _UrllibResponse:
    def __init__(self, response: Any, *, status_code: int, text: str = ""):
        self._response = response
        self.status_code = status_code
        self.text = text

    def iter_lines(self, decode_unicode: bool = True):
        for raw in self._response:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            yield line if decode_unicode else line.encode("utf-8")


def _urllib_post(url: str, *, headers: dict[str, str], json: dict[str, Any], stream: bool, timeout: tuple[int, int]):
    data = __import__("json").dumps(json).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        response = urllib.request.urlopen(request, timeout=max(timeout))
        return _UrllibResponse(response, status_code=getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        return _UrllibResponse([], status_code=exc.code, text=detail)


def task_llm_health(
    *,
    prompt: str = "Return JSON list only: []",
    models: list[str] | None = None,
    profile_paths: Iterable[Path] | None = None,
    request_post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    profile = load_codex_oauth_profile(profile_paths)
    payload: dict[str, Any] = {
        "status": "blocked" if profile is None else "unknown",
        "provider": "openai_codex_oauth",
        "model_candidates": models or task_llm_models(),
        "profile_found": profile is not None,
        "profile_path": profile.path if profile else None,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }
    if profile is None:
        payload.update({"reason": "task_llm_auth_missing"})
        return payload
    try:
        result = generate_codex_text(prompt, models=models, profile=profile, request_post=request_post, timeout_seconds=30)
        payload.update(
            {
                "status": "healthy",
                "reason": "task_llm_ok",
                "model": result.get("model"),
                "attempts": result.get("attempts"),
                "output_hash": hash_text(result.get("text")),
            }
        )
    except AssistantCodexLLMError as exc:
        payload.update(
            {
                "status": "blocked",
                "reason": exc.reason,
                "attempts": sanitize_attempts(exc.attempts),
                "error_hash": exc.error_hash,
            }
        )
    return payload


def sanitize_attempts(attempts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for item in attempts or []:
        safe.append(
            {
                "model": item.get("model"),
                "status": item.get("status"),
                "reason": item.get("reason"),
                "error_hash": item.get("error_hash"),
                "duration_ms": item.get("duration_ms"),
            }
        )
    return safe
