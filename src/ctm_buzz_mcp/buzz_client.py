"""Low-level HTTP client for the Agilix Buzz REST/JSON API.

Environment variables
---------------------
BUZZ_USERNAME  – Buzz login username
BUZZ_PASSWORD  – Buzz login password
BUZZ_DOMAIN    – Buzz domain, e.g. ``ctm.agilixbuzz.com``

The client authenticates once per session and reuses the token for
subsequent requests.  All public methods return parsed JSON dicts.
"""

from __future__ import annotations

import os
from typing import Any

import requests

_DEFAULT_TIMEOUT = 30  # seconds


class BuzzAPIError(Exception):
    """Raised when the Buzz API returns an error response."""


class BuzzClient:
    """Thin wrapper around the Agilix Buzz API.

    Parameters
    ----------
    username : str | None
        Buzz username.  Falls back to ``BUZZ_USERNAME`` env var.
    password : str | None
        Buzz password.  Falls back to ``BUZZ_PASSWORD`` env var.
    domain : str | None
        Buzz domain.  Falls back to ``BUZZ_DOMAIN`` env var.
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
    ) -> None:
        self.username = username or os.environ.get("BUZZ_USERNAME", "")
        self.password = password or os.environ.get("BUZZ_PASSWORD", "")
        self.domain = domain or os.environ.get("BUZZ_DOMAIN", "ctm.agilixbuzz.com")
        self.api_url = f"https://{self.domain}/api"
        self._token: str | None = None
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self) -> str:
        """Authenticate and cache the session token.

        Returns the token string.
        """
        payload = {
            "request": {
                "cmd": "login",
                "username": self.username,
                "password": self.password,
            }
        }
        resp = self._session.post(
            self.api_url, json=payload, timeout=_DEFAULT_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        response_block = data.get("response", {})
        if response_block.get("code") not in (None, "OK", "ok", 0, "0"):
            raise BuzzAPIError(
                f"Login failed: {response_block.get('message', data)}"
            )
        self._token = response_block.get("token", "")
        return self._token

    def _ensure_authenticated(self) -> None:
        if self._token is None:
            self.login()

    # ------------------------------------------------------------------
    # Generic request helper
    # ------------------------------------------------------------------

    def request(self, cmd: str, params: dict[str, Any] | None = None) -> dict:
        """Send *cmd* to the Buzz API and return the parsed JSON response.

        Automatically authenticates if needed.
        """
        self._ensure_authenticated()
        payload: dict[str, Any] = {
            "request": {
                "cmd": cmd,
                "token": self._token,
                **(params or {}),
            }
        }
        resp = self._session.post(
            self.api_url, json=payload, timeout=_DEFAULT_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        response_block = data.get("response", {})
        if response_block.get("code") not in (None, "OK", "ok", 0, "0"):
            raise BuzzAPIError(
                f"API error for '{cmd}': "
                f"{response_block.get('message', data)}"
            )
        return data

    # ------------------------------------------------------------------
    # Convenience methods (thin wrappers around ``request``)
    # ------------------------------------------------------------------

    def list_courses(self, query: str = "") -> dict:
        params: dict[str, Any] = {}
        if query:
            params["query"] = query
        return self.request("listcourses", params)

    def get_course(self, course_id: str) -> dict:
        return self.request("getcourse", {"courseid": course_id})

    def list_assignments(self, course_id: str) -> dict:
        return self.request("listassignments", {"courseid": course_id})

    def get_assignment(self, assignment_id: str) -> dict:
        return self.request("getassignment", {"assignmentid": assignment_id})

    def search_users(self, query: str, role: str = "") -> dict:
        params: dict[str, Any] = {"query": query}
        if role:
            params["role"] = role
        return self.request("listusers", params)

    def get_student_progress(self, student_id: str, course_id: str) -> dict:
        return self.request(
            "getstudentprogress",
            {"studentid": student_id, "courseid": course_id},
        )

    def list_enrollments(self, course_id: str) -> dict:
        return self.request("listenrollments", {"courseid": course_id})

    def get_gradebook(self, course_id: str) -> dict:
        return self.request("getgradebook", {"courseid": course_id})

    def list_announcements(self, course_id: str) -> dict:
        return self.request("listannouncements", {"courseid": course_id})


def get_client() -> BuzzClient:
    """Return a module-level client instance (lazy singleton)."""
    return BuzzClient()
