import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_codex_llm import (
    AssistantCodexLLMError,
    CodexOAuthProfile,
    generate_codex_text,
    load_codex_oauth_profile,
    redact_sensitive,
    task_llm_health,
)


class FakeResponse:
    def __init__(self, *, status_code=200, lines=None, text=""):
        self.status_code = status_code
        self._lines = lines or []
        self.text = text

    def iter_lines(self, decode_unicode=True):
        yield from self._lines


def _profile_file(tmp_path, *, token="secret-access-token", account_id="acct"):
    path = tmp_path / "auth-profiles.json"
    path.write_text(
        json.dumps(
            {
                "profiles": {
                    "openai-codex:default": {
                        "type": "oauth",
                        "provider": "openai-codex",
                        "access": token,
                        "account_id": account_id,
                    }
                }
            }
        )
    )
    return path


def _completed_response(text):
    return [
        "data: "
        + json.dumps(
            {
                "type": "response.completed",
                "response": {"output": [{"content": [{"text": text}]}]},
            }
        ),
        "",
    ]


def test_oauth_profile_discovery_uses_temp_fixture(tmp_path):
    path = _profile_file(tmp_path)

    profile = load_codex_oauth_profile([path])

    assert profile is not None
    assert profile.account_id == "acct"
    assert profile.path == str(path)


def test_oauth_profile_discovery_accepts_openclaw_camel_case_profile(tmp_path):
    path = tmp_path / "auth-profiles.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "openai-codex:dusan@example.com": {
                        "type": "oauth",
                        "provider": "openai-codex",
                        "access": "secret-token",
                        "accountId": "acct-camel",
                    }
                },
            }
        )
    )

    profile = load_codex_oauth_profile([path])

    assert profile is not None
    assert profile.account_id == "acct-camel"


def test_primary_model_success_returns_sanitized_metadata(tmp_path):
    profile = CodexOAuthProfile(access="secret-token", account_id="acct", path=str(_profile_file(tmp_path)))
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"]["model"])
        return FakeResponse(lines=_completed_response('[{"title":"Reply"}]'))

    payload = generate_codex_text("prompt", models=["gpt-5.5"], profile=profile, request_post=post)

    assert payload["status"] == "ok"
    assert payload["model"] == "gpt-5.5"
    assert payload["text"] == '[{"title":"Reply"}]'
    assert calls == ["gpt-5.5"]
    assert "secret-token" not in json.dumps(payload)


def test_fallback_model_is_tried_when_primary_fails(tmp_path):
    profile = CodexOAuthProfile(access="secret-token", account_id="acct", path=str(_profile_file(tmp_path)))
    calls = []

    def post(url, **kwargs):
        model = kwargs["json"]["model"]
        calls.append(model)
        if model == "gpt-5.5":
            return FakeResponse(status_code=500, text="Bearer secret-token failed")
        return FakeResponse(lines=_completed_response("[]"))

    payload = generate_codex_text("prompt", models=["gpt-5.5", "gpt-5.3-mini"], profile=profile, request_post=post)

    assert payload["model"] == "gpt-5.3-mini"
    assert calls == ["gpt-5.5", "gpt-5.3-mini"]
    assert payload["attempts"][0]["error_hash"]
    assert "secret-token" not in json.dumps(payload)


def test_failed_models_raise_classified_safe_error(tmp_path):
    profile = CodexOAuthProfile(access="secret-token", account_id="acct", path=str(_profile_file(tmp_path)))

    def post(url, **kwargs):
        return FakeResponse(status_code=403, text="access_token secret-token denied")

    try:
        generate_codex_text("prompt", models=["gpt-5.5"], profile=profile, request_post=post)
    except AssistantCodexLLMError as exc:
        assert exc.reason == "task_llm_model_failed"
        assert exc.error_hash
        assert "secret-token" not in json.dumps(exc.attempts)
    else:
        raise AssertionError("expected AssistantCodexLLMError")


def test_task_llm_health_reports_auth_missing_without_writes(tmp_path):
    payload = task_llm_health(profile_paths=[tmp_path / "missing.json"])

    assert payload["status"] == "blocked"
    assert payload["reason"] == "task_llm_auth_missing"
    assert payload["calendar_write_attempted"] is False
    assert payload["notion_write_attempted"] is False


def test_redact_sensitive_removes_auth_like_strings():
    text = redact_sensitive("Bearer abc123 token cookie password https://x.test/?token=abc")

    assert "abc123" not in text
    assert "token=abc" not in text
