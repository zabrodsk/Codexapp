# CTM Buzz MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that
integrates with the **Agilix Buzz LMS** at `ctm.agilixbuzz.com`.

The server exposes Buzz API operations as MCP **tools**, **resources**, and
**prompts** so that LLM-powered assistants (Claude Desktop, Cursor, etc.) can
query course, assignment, and student data on your behalf.

> **Read-only by design** – v0.1 only exposes read operations.  No grades are
> modified and no enrollments are changed.

---

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/zabrodsk/Codexapp.git
cd Codexapp
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

### 2. Configure credentials

Copy the example env file and fill in your Buzz credentials:

```bash
cp .env.example .env
# edit .env with your values
```

| Variable        | Description                          |
|-----------------|--------------------------------------|
| `BUZZ_USERNAME` | Your Buzz login username             |
| `BUZZ_PASSWORD` | Your Buzz login password             |
| `BUZZ_DOMAIN`   | Buzz domain (`ctm.agilixbuzz.com`)   |

### 3. Run the server

```bash
# stdio transport (default – for Claude Desktop / Cursor)
ctm-buzz-mcp

# or directly
python -m ctm_buzz_mcp.server
```

### 4. Connect from Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ctm-buzz": {
      "command": "ctm-buzz-mcp"
    }
  }
}
```

---

## Available MCP tools

| Tool                           | Description                                 |
|--------------------------------|---------------------------------------------|
| `search_courses(query)`        | Search courses by keyword                   |
| `get_course(course_id)`        | Get details for one course                  |
| `list_course_assignments(course_id)` | List assignments in a course          |
| `get_assignment_details(assignment_id)` | Get details for one assignment     |
| `search_students(query)`       | Search students by name/username            |
| `get_student_course_progress(student_id, course_id)` | Student progress in a course |

## Available MCP resources

| URI pattern                                       | Description              |
|---------------------------------------------------|--------------------------|
| `buzz://course/{course_id}`                       | Course data              |
| `buzz://assignment/{assignment_id}`               | Assignment data          |
| `buzz://student/{student_id}/progress/{course_id}` | Student progress data   |

## Available MCP prompts

| Prompt                          | Description                                 |
|---------------------------------|---------------------------------------------|
| `student_progress_summary`      | Summarise a student's progress              |
| `assignment_status_summary`     | Summarise assignment statuses for a course  |
| `course_health_check`           | Overall course health report                |

---

## Running tests

```bash
pip install pytest
pytest
```

---

## Architecture

```
src/ctm_buzz_mcp/
├── __init__.py        # Package marker
├── buzz_client.py     # Low-level Buzz REST/JSON client
└── server.py          # FastMCP server (tools, resources, prompts)
```

The Buzz API client (`buzz_client.py`) handles authentication and provides
typed convenience methods.  The MCP server (`server.py`) registers each
method as an MCP tool, exposes key entities as resources, and defines
educator-oriented prompts.

---

## Security notes

* Credentials are loaded from environment variables – **never commit `.env`**.
* v0.1 is **read-only**; no mutation endpoints are exposed.
* All API calls go through HTTPS.
* If you handle student data, ensure compliance with your organisation's
  privacy policies (FERPA, etc.).

---

## License

Private – see repository settings.
