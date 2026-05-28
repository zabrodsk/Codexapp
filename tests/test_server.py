"""Tests for the Buzz API client and MCP server tools.

All HTTP calls are mocked so no real credentials are needed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from ctm_buzz_mcp.buzz_client import BuzzAPIError, BuzzClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client() -> BuzzClient:
    """Return a BuzzClient with dummy credentials (no network)."""
    return BuzzClient(username="testuser", password="testpass", domain="test.agilixbuzz.com")


def _ok_response(payload: dict | None = None) -> MagicMock:
    """Build a mock ``requests.Response`` that looks like a successful Buzz reply."""
    resp = MagicMock()
    resp.status_code = 200
    body = {"response": {"code": "OK", **(payload or {})}}
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    return body, resp


def _login_response(token: str = "tok123") -> MagicMock:
    body = {"response": {"code": "OK", "token": token}}
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# BuzzClient unit tests
# ---------------------------------------------------------------------------


class TestBuzzClientInit:
    def test_defaults_from_params(self, client: BuzzClient) -> None:
        assert client.username == "testuser"
        assert client.password == "testpass"
        assert client.domain == "test.agilixbuzz.com"
        assert client.api_url == "https://test.agilixbuzz.com/api"

    def test_defaults_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BUZZ_USERNAME", "envuser")
        monkeypatch.setenv("BUZZ_PASSWORD", "envpass")
        monkeypatch.setenv("BUZZ_DOMAIN", "env.agilixbuzz.com")
        c = BuzzClient()
        assert c.username == "envuser"
        assert c.password == "envpass"
        assert c.domain == "env.agilixbuzz.com"


class TestBuzzClientLogin:
    def test_login_stores_token(self, client: BuzzClient) -> None:
        mock_resp = _login_response("mytoken")
        with patch.object(client._session, "post", return_value=mock_resp):
            token = client.login()
        assert token == "mytoken"
        assert client._token == "mytoken"

    def test_login_raises_on_error(self, client: BuzzClient) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"response": {"code": "Error", "message": "bad creds"}}
        resp.raise_for_status = MagicMock()
        with patch.object(client._session, "post", return_value=resp):
            with pytest.raises(BuzzAPIError, match="bad creds"):
                client.login()


class TestBuzzClientRequest:
    def test_request_auto_authenticates(self, client: BuzzClient) -> None:
        login_resp = _login_response("tok")
        _, ok_resp = _ok_response({"courses": []})
        ok_mock = MagicMock()
        ok_mock.status_code = 200
        ok_mock.json.return_value = {"response": {"code": "OK", "courses": []}}
        ok_mock.raise_for_status = MagicMock()

        with patch.object(
            client._session,
            "post",
            side_effect=[login_resp, ok_mock],
        ):
            result = client.request("listcourses")

        assert result["response"]["courses"] == []

    def test_request_sends_token(self, client: BuzzClient) -> None:
        client._token = "preexisting"
        _, ok_resp = _ok_response()
        ok_mock = MagicMock()
        ok_mock.status_code = 200
        ok_mock.json.return_value = {"response": {"code": "OK"}}
        ok_mock.raise_for_status = MagicMock()

        with patch.object(client._session, "post", return_value=ok_mock) as mock_post:
            client.request("getcourse", {"courseid": "42"})

        call_kwargs = mock_post.call_args
        sent_json = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert sent_json["request"]["token"] == "preexisting"
        assert sent_json["request"]["cmd"] == "getcourse"
        assert sent_json["request"]["courseid"] == "42"


class TestBuzzClientConvenienceMethods:
    """Ensure convenience wrappers call ``request`` with correct args."""

    def _patched_client(self, client: BuzzClient) -> tuple[BuzzClient, MagicMock]:
        mock_req = MagicMock(return_value={"response": {"code": "OK"}})
        client._token = "tok"
        client.request = mock_req  # type: ignore[assignment]
        return client, mock_req

    def test_list_courses(self, client: BuzzClient) -> None:
        c, mock = self._patched_client(client)
        c.list_courses("math")
        mock.assert_called_once_with("listcourses", {"query": "math"})

    def test_list_courses_no_query(self, client: BuzzClient) -> None:
        c, mock = self._patched_client(client)
        c.list_courses()
        mock.assert_called_once_with("listcourses", {})

    def test_get_course(self, client: BuzzClient) -> None:
        c, mock = self._patched_client(client)
        c.get_course("101")
        mock.assert_called_once_with("getcourse", {"courseid": "101"})

    def test_list_assignments(self, client: BuzzClient) -> None:
        c, mock = self._patched_client(client)
        c.list_assignments("101")
        mock.assert_called_once_with("listassignments", {"courseid": "101"})

    def test_get_assignment(self, client: BuzzClient) -> None:
        c, mock = self._patched_client(client)
        c.get_assignment("a1")
        mock.assert_called_once_with("getassignment", {"assignmentid": "a1"})

    def test_search_users(self, client: BuzzClient) -> None:
        c, mock = self._patched_client(client)
        c.search_users("jane", role="student")
        mock.assert_called_once_with("listusers", {"query": "jane", "role": "student"})

    def test_get_student_progress(self, client: BuzzClient) -> None:
        c, mock = self._patched_client(client)
        c.get_student_progress("s1", "c1")
        mock.assert_called_once_with(
            "getstudentprogress", {"studentid": "s1", "courseid": "c1"}
        )

    def test_list_enrollments(self, client: BuzzClient) -> None:
        c, mock = self._patched_client(client)
        c.list_enrollments("c1")
        mock.assert_called_once_with("listenrollments", {"courseid": "c1"})

    def test_get_gradebook(self, client: BuzzClient) -> None:
        c, mock = self._patched_client(client)
        c.get_gradebook("c1")
        mock.assert_called_once_with("getgradebook", {"courseid": "c1"})

    def test_list_announcements(self, client: BuzzClient) -> None:
        c, mock = self._patched_client(client)
        c.list_announcements("c1")
        mock.assert_called_once_with("listannouncements", {"courseid": "c1"})


# ---------------------------------------------------------------------------
# MCP server tool smoke tests
# ---------------------------------------------------------------------------


class TestMCPTools:
    """Ensure each MCP tool function calls the client correctly."""

    def _mock_client(self) -> MagicMock:
        mock = MagicMock(spec=BuzzClient)
        mock.list_courses.return_value = {"response": {"code": "OK", "courses": []}}
        mock.get_course.return_value = {"response": {"code": "OK", "title": "Math"}}
        mock.list_assignments.return_value = {"response": {"code": "OK", "assignments": []}}
        mock.get_assignment.return_value = {"response": {"code": "OK", "title": "HW1"}}
        mock.search_users.return_value = {"response": {"code": "OK", "users": []}}
        mock.get_student_progress.return_value = {"response": {"code": "OK", "progress": {}}}
        return mock

    @patch("ctm_buzz_mcp.server.get_client")
    def test_search_courses(self, mock_get: MagicMock) -> None:
        from ctm_buzz_mcp.server import search_courses

        mock_get.return_value = self._mock_client()
        result = search_courses("math")
        parsed = json.loads(result)
        assert parsed["response"]["courses"] == []

    @patch("ctm_buzz_mcp.server.get_client")
    def test_get_course(self, mock_get: MagicMock) -> None:
        from ctm_buzz_mcp.server import get_course

        mock_get.return_value = self._mock_client()
        result = get_course("101")
        parsed = json.loads(result)
        assert parsed["response"]["title"] == "Math"

    @patch("ctm_buzz_mcp.server.get_client")
    def test_list_course_assignments(self, mock_get: MagicMock) -> None:
        from ctm_buzz_mcp.server import list_course_assignments

        mock_get.return_value = self._mock_client()
        result = list_course_assignments("101")
        parsed = json.loads(result)
        assert parsed["response"]["assignments"] == []

    @patch("ctm_buzz_mcp.server.get_client")
    def test_get_assignment_details(self, mock_get: MagicMock) -> None:
        from ctm_buzz_mcp.server import get_assignment_details

        mock_get.return_value = self._mock_client()
        result = get_assignment_details("a1")
        parsed = json.loads(result)
        assert parsed["response"]["title"] == "HW1"

    @patch("ctm_buzz_mcp.server.get_client")
    def test_search_students(self, mock_get: MagicMock) -> None:
        from ctm_buzz_mcp.server import search_students

        mock_get.return_value = self._mock_client()
        result = search_students("jane")
        parsed = json.loads(result)
        assert parsed["response"]["users"] == []

    @patch("ctm_buzz_mcp.server.get_client")
    def test_get_student_course_progress(self, mock_get: MagicMock) -> None:
        from ctm_buzz_mcp.server import get_student_course_progress

        mock_get.return_value = self._mock_client()
        result = get_student_course_progress("s1", "c1")
        parsed = json.loads(result)
        assert parsed["response"]["progress"] == {}


# ---------------------------------------------------------------------------
# MCP prompt smoke tests
# ---------------------------------------------------------------------------


class TestMCPPrompts:
    def test_student_progress_summary(self) -> None:
        from ctm_buzz_mcp.server import student_progress_summary

        text = student_progress_summary("s1", "c1")
        assert "s1" in text
        assert "c1" in text

    def test_assignment_status_summary(self) -> None:
        from ctm_buzz_mcp.server import assignment_status_summary

        text = assignment_status_summary("c1")
        assert "c1" in text

    def test_course_health_check(self) -> None:
        from ctm_buzz_mcp.server import course_health_check

        text = course_health_check("c1")
        assert "c1" in text
