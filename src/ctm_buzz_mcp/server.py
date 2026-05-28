"""CTM Buzz MCP Server.

Exposes Agilix Buzz LMS data as MCP tools, resources, and prompts.

Usage
-----
    # stdio transport (default, for Claude Desktop / Cursor / etc.)
    ctm-buzz-mcp

    # or directly
    python -m ctm_buzz_mcp.server
"""

from __future__ import annotations

import json

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from ctm_buzz_mcp.buzz_client import BuzzClient, get_client

load_dotenv()

mcp = FastMCP("ctm-buzz")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client() -> BuzzClient:
    """Return a Buzz API client."""
    return get_client()


def _json_summary(data: dict) -> str:
    """Return a compact JSON string for tool responses."""
    return json.dumps(data, indent=2, default=str)


# ===================================================================
# TOOLS
# ===================================================================


@mcp.tool()
def search_courses(query: str = "") -> str:
    """Search courses in CTM Buzz.

    Parameters
    ----------
    query : str
        Optional search string to filter courses.
    """
    result = _client().list_courses(query)
    return _json_summary(result)


@mcp.tool()
def get_course(course_id: str) -> str:
    """Get detailed information about a single course.

    Parameters
    ----------
    course_id : str
        The Buzz course ID.
    """
    result = _client().get_course(course_id)
    return _json_summary(result)


@mcp.tool()
def list_course_assignments(course_id: str) -> str:
    """List all assignments in a course.

    Parameters
    ----------
    course_id : str
        The Buzz course ID.
    """
    result = _client().list_assignments(course_id)
    return _json_summary(result)


@mcp.tool()
def get_assignment_details(assignment_id: str) -> str:
    """Get detailed information about a single assignment.

    Parameters
    ----------
    assignment_id : str
        The Buzz assignment ID.
    """
    result = _client().get_assignment(assignment_id)
    return _json_summary(result)


@mcp.tool()
def search_students(query: str) -> str:
    """Search students by name or username.

    Parameters
    ----------
    query : str
        Search string (name, username, email, etc.).
    """
    result = _client().search_users(query, role="student")
    return _json_summary(result)


@mcp.tool()
def get_student_course_progress(student_id: str, course_id: str) -> str:
    """Get a single student's progress in a single course.

    Parameters
    ----------
    student_id : str
        The Buzz student/user ID.
    course_id : str
        The Buzz course ID.
    """
    result = _client().get_student_progress(student_id, course_id)
    return _json_summary(result)


# ===================================================================
# RESOURCES
# ===================================================================


@mcp.resource("buzz://course/{course_id}")
def course_resource(course_id: str) -> str:
    """Read-only resource for a course."""
    result = _client().get_course(course_id)
    return _json_summary(result)


@mcp.resource("buzz://assignment/{assignment_id}")
def assignment_resource(assignment_id: str) -> str:
    """Read-only resource for an assignment."""
    result = _client().get_assignment(assignment_id)
    return _json_summary(result)


@mcp.resource("buzz://student/{student_id}/progress/{course_id}")
def student_progress_resource(student_id: str, course_id: str) -> str:
    """Read-only resource for a student's progress in a course."""
    result = _client().get_student_progress(student_id, course_id)
    return _json_summary(result)


# ===================================================================
# PROMPTS
# ===================================================================


@mcp.prompt()
def student_progress_summary(student_id: str, course_id: str) -> str:
    """Generate a prompt that summarises a student's progress.

    The LLM should call the *get_student_course_progress* tool with the
    supplied IDs and then produce a human-friendly summary.
    """
    return (
        f"Please retrieve the progress for student {student_id} in "
        f"course {course_id} using the get_student_course_progress tool, "
        "then provide a clear summary including:\n"
        "- overall completion percentage\n"
        "- grades on completed assignments\n"
        "- any missing or late work\n"
        "- recommendations for the student"
    )


@mcp.prompt()
def assignment_status_summary(course_id: str) -> str:
    """Generate a prompt that summarises assignment statuses for a course.

    The LLM should call *list_course_assignments* and then summarise
    due dates, completion rates, and any overdue items.
    """
    return (
        f"Please retrieve all assignments for course {course_id} using "
        "the list_course_assignments tool, then summarise:\n"
        "- total number of assignments\n"
        "- upcoming due dates\n"
        "- assignments with low completion rates\n"
        "- any overdue assignments"
    )


@mcp.prompt()
def course_health_check(course_id: str) -> str:
    """Generate a prompt for an overall course health check.

    The LLM should call multiple tools to gather course data and then
    produce an executive summary.
    """
    return (
        f"Please perform a health check for course {course_id}:\n"
        "1. Use get_course to retrieve course details.\n"
        "2. Use list_course_assignments to list assignments.\n"
        "3. Summarise the overall health of the course including:\n"
        "   - number of assignments and their status\n"
        "   - any at-risk indicators\n"
        "   - recommendations for the instructor"
    )


# ===================================================================
# Entry-point
# ===================================================================


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
