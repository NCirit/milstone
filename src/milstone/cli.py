"""Command-line interface for the Milstone tool."""
from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import multiprocessing
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib import error as urllib_error, request as urllib_request

import typer
from rich import print as rprint
from rich.table import Table
from rich.tree import Tree

from . import state

app = typer.Typer(help="Manage milestones via CLI and web interface")
project_app = typer.Typer(help="Project-level commands")
milestone_app = typer.Typer(help="Create, update, and list milestones")
log_app = typer.Typer(help="Manage milestone logs")
progress_app = typer.Typer(help="Progress tracking commands")
service_app = typer.Typer(help="Background service utilities")

STATE_DIR_NAME = ".milstone"
DB_FILENAME = "milstone.db"
STATUS_MD_FILENAME = "milstone_status.md"
LLM_USAGE_FILENAME = "llm_instructions.txt"
LLM_TEMPLATE_PATH = Path(__file__).resolve().parent / "data" / "llm_instructions_template.txt"
SERVER_MODULE_PATH = "milstone.server"
DEFAULT_EXPECTED_HOURS = 1.0
MILSTONE_SERVER_PORT = 8123  # Hardcoded port for Milstone server
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS milestones (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    priority INTEGER NOT NULL DEFAULT 3,
    owner TEXT,
    start_date TEXT,
    due_date TEXT,
    completed_at TEXT,
    parent_id INTEGER REFERENCES milestones(id) ON DELETE SET NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    expected_hours REAL NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, slug)
);

CREATE TABLE IF NOT EXISTS progress_snapshots (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    total_hours REAL NOT NULL,
    completed_hours REAL NOT NULL,
    total_count INTEGER NOT NULL,
    completed_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS milestone_updates (
    id INTEGER PRIMARY KEY,
    milestone_id INTEGER NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    author TEXT,
    summary TEXT NOT NULL,
    status TEXT,
    progress INTEGER,
    sequence INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS milestone_dependencies (
    id INTEGER PRIMARY KEY,
    milestone_id INTEGER NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    depends_on_id INTEGER NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    relation TEXT NOT NULL DEFAULT 'blocks',
    UNIQUE(milestone_id, depends_on_id)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS milestone_tags (
    milestone_id INTEGER NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (milestone_id, tag_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _maybe_add_column(conn: sqlite3.Connection, table: str, column: str, column_sql: str) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql}")
        conn.commit()


def _migrate_old_project_keys(conn: sqlite3.Connection) -> None:
    """Migrate old hardcoded project keys (e.g., 'main', 'default') to UUIDs."""
    # Check if there are any projects with non-UUID keys
    # A simple heuristic: if the key doesn't contain a hyphen, it's probably old
    rows = conn.execute("SELECT id, key FROM projects WHERE key NOT LIKE '%-%'").fetchall()

    if not rows:
        return  # No old keys to migrate

    with conn:
        for row in rows:
            old_key = row["key"]
            new_key = str(uuid.uuid4())
            conn.execute("UPDATE projects SET key = ? WHERE id = ?", (new_key, row["id"]))
            typer.echo(f"Migrated project key from '{old_key}' to '{new_key}'")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    _maybe_add_column(conn, "milestones", "parent_id", "parent_id INTEGER REFERENCES milestones(id) ON DELETE SET NULL")
    _maybe_add_column(conn, "milestones", "deleted", "deleted INTEGER NOT NULL DEFAULT 0")
    _maybe_add_column(conn, "milestones", "expected_hours", "expected_hours REAL NOT NULL DEFAULT 1")
    _maybe_add_column(conn, "milestone_updates", "sequence", "sequence INTEGER")
    _ensure_log_sequences(conn)
    _normalize_statuses(conn)
    _migrate_old_project_keys(conn)


def _state_dir(base_path: Path) -> Path:
    return base_path / STATE_DIR_NAME


def _find_state_dir(start: Path) -> Optional[Path]:
    for candidate in [start, *start.parents]:
        candidate_state = candidate / STATE_DIR_NAME
        if candidate_state.exists():
            return candidate_state
    return None


def _db_path(base_path: Path) -> Path:
    return _state_dir(base_path) / DB_FILENAME


LLM_FALLBACK_TEXT = """
Milstone CLI – LLM Usage Notes
================================

This file is generated automatically inside each project's `.milstone/` directory so language models
understand how to operate the tool safely. Run any command with `--help` to see the complete option set before invoking it.

Setup & Project Management
--------------------------
1. `milstone project init "Project Name" PATH` – create `.milstone/` and seed the default project (override `--project-key` if needed).
2. `milstone project report [--project-key KEY] [--output PATH]` – write "{status_md}" describing the active progress term.
3. `milstone project ui [--project-key KEY]` – launch the background Flask UI / web dashboard.

Milestone Lifecycle
-------------------
* `milstone milestone add "Title" --status active --expected-hours 2 --parent parent-slug` – create work items (status defaults to "active").
* `milstone milestone update <slug>` with flags such as `--status`, `--parent` / `--clear-parent`, `--deleted/--undeleted`, `--expected-hours`.
* `milstone milestone list [--status ...] [--include-deleted]` – inspect the milestone tree via the CLI.

Logging Progress
----------------
* `milstone log add <slug> "Short summary"` – append textual updates.
* `milstone log list <slug>` – review existing updates.
* `milstone log edit <slug> --index N --summary "Revised note"` (or `--log-id ID`).

Reporting & Tracking
--------------------
* `milstone project report` produces the Markdown snapshot consumed by dashboards.
* `milstone progress show | reset | history` – inspect period totals, reset them, or review prior snapshots.
* `milstone service start|stop|restart --port PORT` – manage the background web service without opening the UI.

Remember: the CLI is self-documenting via `--help`. Encourage models to inspect command-specific help before invoking unfamiliar options.
"""


def _dump_llm_usage(target_dir: Path) -> None:
    llm_file = target_dir / LLM_USAGE_FILENAME
    try:
        template = LLM_TEMPLATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        template = LLM_FALLBACK_TEXT
    llm_file.write_text(
        template.format(status_md=STATUS_MD_FILENAME),
        encoding="utf-8",
    )


def _ensure_flask_available() -> None:
    try:
        import flask  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    try:
        import ensurepip

        ensurepip.bootstrap()
    except Exception:
        pass

    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "flask>=3.0"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # pragma: no cover
        raise typer.BadParameter(
            "Flask is not installed for this interpreter. Run `pip install -e .` or `pip install flask`."
        ) from exc


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _slugify(title: str) -> str:
    slug = _SLUG_PATTERN.sub("-", title.lower()).strip("-")
    return slug or "milestone"


def _generate_slug(conn: sqlite3.Connection, project_id: int, title: str) -> str:
    base = _slugify(title)
    slug = base
    counter = 2
    while conn.execute(
        "SELECT 1 FROM milestones WHERE project_id = ? AND slug = ?",
        (project_id, slug),
    ).fetchone():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def _connect_existing(base_path: Path) -> sqlite3.Connection:
    """Open an existing project database or raise if init not run."""
    db_path = _db_path(base_path)
    if not db_path.exists():
        raise typer.BadParameter("Missing .milstone database. Run `milstone project init` first.")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_schema(conn)
    return conn


def _ensure_project(conn: sqlite3.Connection, key: str, name: Optional[str] = None, description: Optional[str] = None) -> int:
    """Ensure a project row exists and return its id."""
    row = conn.execute("SELECT id FROM projects WHERE key = ?", (key,)).fetchone()
    if row:
        return row[0]
    cursor = conn.execute(
        "INSERT INTO projects (key, name, description) VALUES (?, ?, ?)",
        (key, name or key, description),
    )
    conn.commit()
    return cursor.lastrowid


def _get_project_id(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT id FROM projects WHERE key = ?", (key,)).fetchone()
    if not row:
        raise typer.BadParameter(f"Project '{key}' not found. Run `milstone project init` first.")
    return row[0]


def _get_single_project_id(conn: sqlite3.Connection) -> int:
    """Get the ID of the single project in this .milstone folder."""
    row = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()
    if not row:
        raise typer.BadParameter("No project found. Run `milstone project init` first.")
    return row[0]


def _ensure_log_sequences(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, milestone_id FROM milestone_updates WHERE sequence IS NULL ORDER BY milestone_id, created_at, id"
    ).fetchall()
    if not rows:
        return
    counters: Dict[int, int] = {}
    with conn:
        for row in rows:
            milestone_id = row["milestone_id"]
            counters[milestone_id] = counters.get(milestone_id, 0) + 1
            conn.execute("UPDATE milestone_updates SET sequence = ? WHERE id = ?", (counters[milestone_id], row["id"]))


def _normalize_statuses(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("UPDATE milestones SET status = 'active' WHERE status = 'planned'")
        conn.execute("UPDATE milestones SET status = 'done' WHERE status = 'completed'")


def _canonical_status(value: Optional[str]) -> str:
    if not value:
        return "active"
    value = value.strip().lower()
    if value == "planned":
        return "active"
    if value == "completed":
        return "done"
    return value


def _auto_completed_at(status: str, existing: Optional[str]) -> Optional[str]:
    status = _canonical_status(status)
    if status == "done":
        return datetime.now(timezone.utc).isoformat()
    return None


def _lookup_milestone(
    conn: sqlite3.Connection,
    project_id: int,
    slug: str,
    include_deleted: bool = False,
) -> Optional[sqlite3.Row]:
    query = "SELECT * FROM milestones WHERE project_id = ? AND slug = ?"
    params: List[object] = [project_id, slug]
    if not include_deleted:
        query += " AND deleted = 0"
    return conn.execute(query, params).fetchone()


def _next_log_sequence(conn: sqlite3.Connection, milestone_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) FROM milestone_updates WHERE milestone_id = ?",
        (milestone_id,),
    ).fetchone()
    return (row[0] or 0) + 1


def _insert_log_entry(
    conn: sqlite3.Connection,
    milestone_id: int,
    summary: str,
) -> tuple[int, int]:
    summary = (summary or "").strip()
    if not summary:
        raise typer.BadParameter("Summary is required for a log entry.")
    sequence = _next_log_sequence(conn, milestone_id)
    with conn:
        cursor = conn.execute(
            "INSERT INTO milestone_updates (milestone_id, summary, sequence) VALUES (?, ?, ?)",
            (milestone_id, summary, sequence),
        )
    return cursor.lastrowid, sequence


def _log_row_by_identifier(
    conn: sqlite3.Connection,
    milestone_id: int,
    *,
    log_id: Optional[int] = None,
    sequence: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    if log_id is not None:
        return conn.execute(
            "SELECT * FROM milestone_updates WHERE milestone_id = ? AND id = ?",
            (milestone_id, log_id),
        ).fetchone()
    if sequence is not None:
        return conn.execute(
            "SELECT * FROM milestone_updates WHERE milestone_id = ? AND sequence = ?",
            (milestone_id, sequence),
        ).fetchone()
    return None


def _update_log_entry(
    conn: sqlite3.Connection,
    milestone_id: int,
    *,
    log_id: Optional[int] = None,
    sequence: Optional[int] = None,
    summary: Optional[str] = None,
) -> None:
    row = _log_row_by_identifier(conn, milestone_id, log_id=log_id, sequence=sequence)
    if row is None:
        raise typer.BadParameter("Log entry not found for the specified identifier.")
    updates: Dict[str, object] = {}
    if summary not in (None, ""):
        updates["summary"] = summary
    if not updates:
        raise typer.BadParameter("No log updates provided.")
    set_clause = ", ".join(f"{column} = ?" for column in updates)
    values = list(updates.values())
    values.extend([milestone_id, row["id"]])
    with conn:
        conn.execute(
            f"UPDATE milestone_updates SET {set_clause} WHERE milestone_id = ? AND id = ?",
            values,
        )


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if "T" not in value and " " not in value:
        value = f"{value}T00:00:00"
    else:
        value = value.replace(" ", "T")
    if value.endswith("Z"):
        value = value[:-1]
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _milestone_window(row: sqlite3.Row) -> tuple[Optional[datetime], Optional[datetime]]:
    start = _parse_datetime(row["start_date"]) or _parse_datetime(row["created_at"])
    end = _parse_datetime(row["completed_at"]) or _parse_datetime(row["due_date"])
    return start, end


def _latest_snapshot(conn: sqlite3.Connection, project_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM progress_snapshots WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()


def _current_period_start(conn: sqlite3.Connection, project_id: int) -> Optional[datetime]:
    snapshot = _latest_snapshot(conn, project_id)
    if snapshot is None:
        return None
    return _parse_datetime(snapshot["created_at"])


def _milestone_in_period(row: sqlite3.Row, since: Optional[datetime]) -> bool:
    if since is None:
        return True
    start, end = _milestone_window(row)
    now_dt = datetime.now(timezone.utc)
    effective_end = end or now_dt
    effective_start = start or effective_end
    return effective_end >= since and effective_start <= now_dt


def _progress_stats(conn: sqlite3.Connection, project_id: int, since: Optional[datetime]) -> Dict[str, float]:
    rows = conn.execute(
        "SELECT expected_hours, status, start_date, due_date, completed_at, created_at, deleted"
        " FROM milestones WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    filtered = [row for row in rows if not row["deleted"] and _milestone_in_period(row, since)]
    total_hours = sum(row["expected_hours"] for row in filtered)
    completed = [row for row in filtered if row["status"] == "done"]
    completed_hours = sum(row["expected_hours"] for row in completed)
    total_count = len(filtered)
    completed_count = len(completed)
    ratio = (completed_hours / total_hours) if total_hours else 0.0
    return {
        "total_hours": total_hours,
        "completed_hours": completed_hours,
        "total_count": total_count,
        "completed_count": completed_count,
        "ratio": ratio,
    }


def _record_snapshot(conn: sqlite3.Connection, project_id: int, label: Optional[str]) -> sqlite3.Row:
    since = _current_period_start(conn, project_id)
    stats = _progress_stats(conn, project_id, since)
    snapshot_label = label or f"Reset {_today_iso()}"
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO progress_snapshots (
                project_id, label, total_hours, completed_hours, total_count, completed_count
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                snapshot_label,
                stats["total_hours"],
                stats["completed_hours"],
                stats["total_count"],
                stats["completed_count"],
            ),
        )
    return conn.execute("SELECT * FROM progress_snapshots WHERE id = ?", (cursor.lastrowid,)).fetchone()




def _server_log_path() -> Path:
    """Get the path to the server log file."""
    return state.global_runtime_dir() / "server.log"


def _ping_server(port: int, timeout: float = 0.5) -> bool:
    try:
        with urllib_request.urlopen(f"http://127.0.0.1:{port}/__health", timeout=timeout) as response:
            return response.status == 200
    except (urllib_error.URLError, urllib_error.HTTPError, ConnectionError):
        return False


def _start_server_process(port: int) -> subprocess.Popen:
    _ensure_flask_available()
    server_log = _server_log_path()
    server_log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        SERVER_MODULE_PATH,
        "--port",
        str(port),
    ]
    # Open log file for the subprocess - don't close it, let the subprocess own it
    # The file will be closed when the subprocess terminates
    log_handle = open(server_log, "a", encoding="utf-8", buffering=1)  # Line buffered
    return subprocess.Popen(
        cmd,
        stdout=log_handle,
        stderr=log_handle,
        stdin=subprocess.DEVNULL,
        close_fds=os.name != "nt",
        start_new_session=os.name != "nt",
    )


def _get_or_start_server() -> int:
    """
    Get existing server or start a new one on the hardcoded port.
    Uses simple health check to detect running server.

    Returns:
        Port number (always MILSTONE_SERVER_PORT)

    Raises:
        typer.BadParameter: If server fails to start
    """
    # Check if server is already running via health check
    if _ping_server(MILSTONE_SERVER_PORT):
        return MILSTONE_SERVER_PORT

    # No running server found, start a new one
    process = _start_server_process(MILSTONE_SERVER_PORT)
    start = time.time()
    while time.time() - start < 5:
        if process.poll() is not None:
            # Process terminated, check if it was an error
            break
        if _ping_server(MILSTONE_SERVER_PORT):
            # Server is up and responding
            return MILSTONE_SERVER_PORT
        time.sleep(0.2)

    # Server failed to start
    process.terminate()
    log_path = _server_log_path()
    raise typer.BadParameter(f"Failed to start Milstone web server. Check log file for details:\n{log_path}")


def _record_project_history(state_dir: Path, entry: Dict[str, str]) -> None:
    state.record_project_open(entry)


def _fetch_project_info(conn: sqlite3.Connection, project_id: int) -> Dict[str, Optional[str]]:
    row = conn.execute("SELECT key, name, description FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise typer.BadParameter("Project metadata missing.")
    return {"key": row["key"], "name": row["name"], "description": row["description"]}




def _shutdown_service() -> bool:
    """
    Shutdown the Milstone server on the hardcoded port.

    Returns:
        True if graceful shutdown succeeded, False otherwise
    """
    # Check if server is running
    if not _ping_server(MILSTONE_SERVER_PORT):
        return False

    # Request graceful shutdown
    request_obj = urllib_request.Request(
        f"http://127.0.0.1:{MILSTONE_SERVER_PORT}/__stop",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib_request.urlopen(request_obj, timeout=1) as resp:
            if resp.status == 200:
                # Wait for server to stop (up to 5 seconds)
                for _ in range(25):
                    if not _ping_server(MILSTONE_SERVER_PORT):
                        return True
                    time.sleep(0.2)
    except Exception:
        pass

    return False


def _register_project_with_server(
    port: int,
    project_info: Dict[str, Optional[str]],
    project_root: Path,
    state_dir: Path,
) -> None:
    payload = {
        "projectKey": project_info["key"],
        "name": project_info.get("name"),
        "description": project_info.get("description"),
        "path": str(project_root),
        "stateDir": str(state_dir),
    }
    data = json.dumps(payload).encode("utf-8")
    request_obj = urllib_request.Request(
        f"http://127.0.0.1:{port}/api/projects/register",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib_request.urlopen(request_obj, timeout=5):
            return
    except urllib_error.HTTPError as exc:
        log_path = _server_log_path()
        raise typer.BadParameter(
            f"Failed to register project with server: {exc.read().decode()}\n"
            f"Check server log for details: {log_path}"
        ) from exc
    except urllib_error.URLError as exc:
        log_path = _server_log_path()
        raise typer.BadParameter(
            f"Unable to contact Milstone server for registration.\n"
            f"Check server log for details: {log_path}"
        ) from exc


@project_app.command("init")
def project_init(
    project_name: str = typer.Argument(..., help="Project name"),
    path: Path = typer.Argument(Path("."), help="Project root to initialize"),
    description: Optional[str] = typer.Option(None, "--description", help="Optional description for the project"),
) -> None:
    """Initialize milestone tracking artifacts."""

    project_root = path.resolve()
    typer.echo(f"Initializing Milstone in {project_root}")
    state_dir = _state_dir(project_root)
    state_dir.mkdir(parents=True, exist_ok=True)

    # Generate a unique UUID for this project
    project_key = str(uuid.uuid4())

    conn = sqlite3.connect(_db_path(project_root))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        _ensure_schema(conn)
        _ensure_project(conn, project_key, name=project_name, description=description)
    finally:
        conn.close()

    _dump_llm_usage(state_dir)
    typer.echo(f"Initialization complete: .milstone assets ready for '{project_name}'.")
    typer.echo(f"Note: You can customize the 'User Instructions' section in .milstone/{LLM_USAGE_FILENAME} to add your own guidelines for LLM models.")


@project_app.command("report")
def project_report(
    path: Path = typer.Option(Path("."), "--path", help="Project root"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Path for rendered markdown (defaults to CWD/milstone_status.md)"),
) -> None:
    """Generate markdown summary of the current progress term."""

    project_root = path.resolve()
    conn = _connect_existing(project_root)
    try:
        project_id = _resolve_project_fields(conn, None, None, create_if_missing=False)
        project_info = _fetch_project_info(conn, project_id)
        since = _current_period_start(conn, project_id)
        progress = _progress_stats(conn, project_id, since)
        forest = _build_milestone_forest(conn, project_id, since)
        active_nodes = _collect_active_nodes(forest)
        completed_nodes = _collect_completed_nodes(forest)
        markdown = _render_report_markdown(project_info, progress, since, active_nodes, completed_nodes)
    finally:
        conn.close()

    output_path = (output if output is not None else Path.cwd() / STATUS_MD_FILENAME).resolve()
    output_path.write_text(markdown, encoding="utf-8")
    typer.echo(f"Wrote {output_path}")


def _resolve_project_fields(
    conn: sqlite3.Connection,
    project_name: Optional[str],
    project_description: Optional[str],
    create_if_missing: bool,
) -> int:
    """Get the project ID for the single project in this .milstone folder.

    If create_if_missing is True and no project exists, creates one with a UUID key.
    """
    if create_if_missing:
        # Check if project already exists
        row = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()
        if row:
            return row[0]
        # Create new project with UUID key
        project_key = str(uuid.uuid4())
        return _ensure_project(conn, project_key, project_name, project_description)
    return _get_single_project_id(conn)


@milestone_app.command("add")
def create_milestone(
    title: str = typer.Argument(..., help="Human readable title"),
    path: Path = typer.Option(Path("."), "--path", help="Project root containing .milstone"),
    project_name: Optional[str] = typer.Option(None, "--project-name", help="Project name (used if project needs to be created)"),
    project_description: Optional[str] = typer.Option(None, "--project-description", help="Project description if a new project is created"),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Additional context or markdown"),
    status: str = typer.Option("active", "--status", "-s", help="Milestone status"),
    priority: int = typer.Option(3, "--priority", "-r", help="Priority (1=highest)"),
    owner: Optional[str] = typer.Option(None, "--owner", help="Owner or responsible person"),
    start_date: Optional[str] = typer.Option(None, "--start-date", help="ISO formatted start date"),
    due_date: Optional[str] = typer.Option(None, "--due-date", help="ISO formatted due date"),
    parent: Optional[str] = typer.Option(None, "--parent", "-P", help="Slug of the parent milestone"),
    expected_hours: float = typer.Option(DEFAULT_EXPECTED_HOURS, "--expected-hours", help="Estimated hours required for completion"),
) -> None:
    """Create a milestone entry."""

    if expected_hours <= 0:
        raise typer.BadParameter("Expected hours must be positive.")

    status = _canonical_status(status)
    project_root = path.resolve()
    conn = _connect_existing(project_root)
    try:
        project_id = _resolve_project_fields(conn, project_name, project_description, create_if_missing=True)
        parent_id: Optional[int] = None
        slug = _generate_slug(conn, project_id, title)
        if parent:
            parent_row = _lookup_milestone(conn, project_id, parent)
            if parent_row is None:
                raise typer.BadParameter(f"Parent milestone '{parent}' not found or deleted.")
            parent_id = parent_row["id"]
        if parent == slug:
            raise typer.BadParameter("A milestone cannot be its own parent.")
        completed_at_value = _auto_completed_at(status, None)
        with conn:
            conn.execute(
                """
                INSERT INTO milestones (
                    project_id, slug, title, description, status, priority, owner, start_date, due_date, parent_id, expected_hours, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    slug,
                    title,
                    description,
                    status,
                    priority,
                    owner,
                    start_date,
                    due_date,
                    parent_id,
                    expected_hours,
                    completed_at_value,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise typer.BadParameter(f"Milestone '{slug}' already exists.") from exc
    finally:
        conn.close()

    typer.echo(f"Created milestone '{slug}'.")


@milestone_app.command("update")
def update_milestone(
    slug: str = typer.Argument(..., help="Slug of the milestone to update"),
    path: Path = typer.Option(Path("."), "--path", help="Project root containing .milstone"),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="New title"),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="New description"),
    status: Optional[str] = typer.Option(None, "--status", "-s", help="New status"),
    priority: Optional[int] = typer.Option(None, "--priority", "-r", help="New priority"),
    owner: Optional[str] = typer.Option(None, "--owner", help="New owner"),
    start_date: Optional[str] = typer.Option(None, "--start-date", help="New start date"),
    due_date: Optional[str] = typer.Option(None, "--due-date", help="New due date"),
    completed_at: Optional[str] = typer.Option(None, "--completed-at", help="ISO timestamp when milestone completed"),
    parent: Optional[str] = typer.Option(None, "--parent", "-P", help="Set/replace parent milestone by slug"),
    clear_parent: bool = typer.Option(False, "--clear-parent", help="Remove the parent link"),
    deleted_flag: Optional[bool] = typer.Option(None, "--deleted/--undeleted", help="Soft delete or restore the milestone"),
    expected_hours: Optional[float] = typer.Option(None, "--expected-hours", help="Update expected hours"),
) -> None:
    """Update fields on an existing milestone."""

    if parent and clear_parent:
        raise typer.BadParameter("Cannot specify both --parent and --clear-parent.")
    if parent and parent == slug:
        raise typer.BadParameter("A milestone cannot be its own parent.")
    if expected_hours is not None and expected_hours <= 0:
        raise typer.BadParameter("Expected hours must be positive.")

    project_root = path.resolve()
    conn = _connect_existing(project_root)
    try:
        project_id = _resolve_project_fields(conn, None, None, create_if_missing=False)
        existing = _lookup_milestone(conn, project_id, slug, include_deleted=True)
        if existing is None:
            raise typer.BadParameter(f"Milestone '{slug}' not found.")
        updates: Dict[str, object] = {}
        for field_name, value in (
            ("title", title),
            ("description", description),
            ("status", status),
            ("priority", priority),
            ("owner", owner),
            ("start_date", start_date),
            ("due_date", due_date),
            ("completed_at", completed_at),
        ):
            if value is not None:
                updates[field_name] = _canonical_status(value) if field_name == "status" else value

        if parent:
            parent_row = _lookup_milestone(conn, project_id, parent)
            if parent_row is None:
                raise typer.BadParameter(f"Parent milestone '{parent}' not found or deleted.")
            updates["parent_id"] = parent_row["id"]
        elif clear_parent:
            updates["parent_id"] = None

        if deleted_flag is not None:
            updates["deleted"] = 1 if deleted_flag else 0
        if expected_hours is not None:
            updates["expected_hours"] = expected_hours
        if not updates:
            typer.echo("No updates specified; nothing to do.")
            raise typer.Exit(code=0)
        if "status" in updates:
            new_status = updates["status"]
            if new_status == "done":
                if "completed_at" not in updates or updates["completed_at"] in (None, ""):
                    updates["completed_at"] = _auto_completed_at(new_status, existing["completed_at"])
            else:
                updates["completed_at"] = None

        set_fragments = [f"{column} = ?" for column in updates]
        values = list(updates.values())
        set_fragments.append("updated_at = CURRENT_TIMESTAMP")
        values.extend([project_id, slug])
        with conn:
            conn.execute(
                f"UPDATE milestones SET {', '.join(set_fragments)} WHERE project_id = ? AND slug = ?",
                values,
            )
    finally:
        conn.close()

    typer.echo(f"Updated milestone '{slug}'.")


@milestone_app.command("list")
def list_milestones(
    path: Path = typer.Option(Path("."), "--path", help="Project root containing .milstone"),
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status"),
    include_done: bool = typer.Option(True, "--include-done/--exclude-done", help="Whether to include completed milestones"),
    include_deleted: bool = typer.Option(False, "--include-deleted", help="Include soft-deleted milestones in the output"),
) -> None:
    """List milestones for a project."""

    project_root = path.resolve()
    conn = _connect_existing(project_root)
    try:
        project_id = _resolve_project_fields(conn, None, None, create_if_missing=False)
        project_info = _fetch_project_info(conn, project_id)
        since = _current_period_start(conn, project_id)
        query = [
            "SELECT id, parent_id, slug, title, status, priority, owner, start_date, due_date, completed_at, deleted, expected_hours, created_at",
            "FROM milestones",
            "WHERE project_id = ?",
        ]
        params = [project_id]
        if status:
            status = _canonical_status(status)
            query.append("AND status = ?")
            params.append(status)
        if not include_done:
            query.append("AND status != 'done'")
        if not include_deleted:
            query.append("AND deleted = 0")
        query.append("ORDER BY priority ASC, due_date IS NULL, due_date")
        rows = conn.execute(" ".join(query), params).fetchall()
        if since:
            rows = [row for row in rows if _milestone_in_period(row, since)]
    finally:
        conn.close()

    if not rows:
        typer.echo("No milestones found.")
        raise typer.Exit(code=0)

    children: Dict[int, List[sqlite3.Row]] = {row["id"]: [] for row in rows}
    roots: List[sqlite3.Row] = []
    for row in rows:
        parent_id = row["parent_id"]
        if parent_id and parent_id in children:
            children[parent_id].append(row)
        else:
            roots.append(row)

    tree = Tree(f"[bold]Milestones ({project_info['name']})[/bold]")

    def _label(row: sqlite3.Row) -> str:
        display_status = "deleted" if row["deleted"] else row["status"]
        pieces = [f"[cyan]{row['slug']}[/cyan]", row["title"], f"[magenta]{display_status}[/magenta]"]
        if row["owner"]:
            pieces.append(f"owner: {row['owner']}")
        if row["due_date"]:
            pieces.append(f"due: {row['due_date']}")
        pieces.append(f"{row['expected_hours']}h")
        return " • ".join(str(piece) for piece in pieces if piece)

    def _add_children(branch: Tree, node: sqlite3.Row) -> None:
        child_branch = branch.add(_label(node))
        for child in children.get(node["id"], []):
            _add_children(child_branch, child)

    if not roots:
        roots = rows  # degrade to flat list if tree cannot be built (e.g., parent filtered out)

    for root in roots:
        _add_children(tree, root)

    rprint(tree)


app.add_typer(project_app, name="project")
app.add_typer(milestone_app, name="milestone")
app.add_typer(log_app, name="log")


@log_app.command("add")
def logs_add(
    slug: str = typer.Argument(..., help="Milestone slug"),
    summary: str = typer.Argument(..., help="Log summary"),
    path: Path = typer.Option(Path("."), "--path", help="Project root"),
) -> None:
    """Add a log entry to a milestone."""

    project_root = path.resolve()
    conn = _connect_existing(project_root)
    try:
        project_id = _resolve_project_fields(conn, None, None, create_if_missing=False)
        milestone = _lookup_milestone(conn, project_id, slug, include_deleted=True)
        if milestone is None:
            raise typer.BadParameter(f"Milestone '{slug}' not found.")
        log_id, sequence = _insert_log_entry(conn, milestone["id"], summary)
    finally:
        conn.close()

    typer.echo(f"Added log #{sequence} (id {log_id}) to milestone '{slug}'.")


@log_app.command("list")
def logs_list(
    slug: str = typer.Argument(..., help="Milestone slug"),
    path: Path = typer.Option(Path("."), "--path", help="Project root"),
) -> None:
    """List logs for a milestone."""

    project_root = path.resolve()
    conn = _connect_existing(project_root)
    try:
        project_id = _resolve_project_fields(conn, None, None, create_if_missing=False)
        milestone = _lookup_milestone(conn, project_id, slug, include_deleted=True)
        if milestone is None:
            raise typer.BadParameter(f"Milestone '{slug}' not found.")
        rows = conn.execute(
            "SELECT id, sequence, author, summary, status, progress, created_at FROM milestone_updates "
            "WHERE milestone_id = ? ORDER BY sequence ASC",
            (milestone["id"],),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("No logs recorded for this milestone.")
        return

    table = Table(title=f"Logs for {slug}")
    table.add_column("#")
    table.add_column("Summary")
    table.add_column("Created")
    for row in rows:
        table.add_row(
            str(row["sequence"]),
            row["summary"],
            row["created_at"],
        )
    rprint(table)


@log_app.command("edit")
def logs_edit(
    slug: str = typer.Argument(..., help="Milestone slug"),
    path: Path = typer.Option(Path("."), "--path", help="Project root"),
    summary: Optional[str] = typer.Option(None, "--summary", help="Updated summary"),
    index: Optional[int] = typer.Option(None, "--index", "-i", help="Log sequence number"),
    log_id: Optional[int] = typer.Option(None, "--log-id", help="Explicit log row id"),
) -> None:
    """Edit an existing log entry for a milestone."""

    if index is None and log_id is None:
        raise typer.BadParameter("Provide either --index or --log-id to identify the log entry.")

    project_root = path.resolve()
    conn = _connect_existing(project_root)
    try:
        project_id = _resolve_project_fields(conn, None, None, create_if_missing=False)
        milestone = _lookup_milestone(conn, project_id, slug, include_deleted=True)
        if milestone is None:
            raise typer.BadParameter(f"Milestone '{slug}' not found.")
        _update_log_entry(
            conn,
            milestone["id"],
            log_id=log_id,
            sequence=index,
            summary=summary,
        )
    finally:
        conn.close()

    typer.echo("Log entry updated.")
def _format_stats(stats: Dict[str, float]) -> str:
    percent = round(stats["ratio"] * 100, 2) if stats["ratio"] else 0.0
    return (
        f"{stats['completed_hours']:.2f}h / {stats['total_hours']:.2f}h "
        f"({stats['completed_count']} of {stats['total_count']} milestones → {percent}%)"
    )


@progress_app.command("show")
def progress_show(
    path: Path = typer.Option(Path("."), "--path", help="Project root"),
) -> None:
    """Display progress for the current period (since the last reset)."""

    project_root = path.resolve()
    conn = _connect_existing(project_root)
    try:
        project_id = _resolve_project_fields(conn, None, None, create_if_missing=False)
        since = _current_period_start(conn, project_id)
        stats = _progress_stats(conn, project_id, since)
    finally:
        conn.close()

    since_label = since.isoformat() if since else "project start"
    typer.echo(f"Progress since {since_label}: {_format_stats(stats)}")


@progress_app.command("reset")
def progress_reset(
    path: Path = typer.Option(Path("."), "--path", help="Project root"),
    label: Optional[str] = typer.Option(None, "--label", "-l", help="Label for the saved snapshot"),
) -> None:
    """Save the current progress stats and start a new period."""

    project_root = path.resolve()
    conn = _connect_existing(project_root)
    try:
        project_id = _resolve_project_fields(conn, None, None, create_if_missing=False)
        snapshot = _record_snapshot(conn, project_id, label)
    finally:
        conn.close()

    typer.echo(
        f"Saved snapshot '{snapshot['label']}' ({snapshot['completed_hours']:.2f}h / {snapshot['total_hours']:.2f}h, "
        f"{snapshot['completed_count']} of {snapshot['total_count']} milestones)."
    )


@progress_app.command("history")
def progress_history(
    path: Path = typer.Option(Path("."), "--path", help="Project root"),
) -> None:
    """List saved progress snapshots."""

    project_root = path.resolve()
    conn = _connect_existing(project_root)
    try:
        project_id = _resolve_project_fields(conn, None, None, create_if_missing=False)
        project_info = _fetch_project_info(conn, project_id)
        rows = conn.execute(
            "SELECT label, created_at, total_hours, completed_hours, total_count, completed_count "
            "FROM progress_snapshots WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("No snapshots found.")
        return

    table = Table(title=f"Progress snapshots ({project_info['name']})")
    table.add_column("Created", style="cyan")
    table.add_column("Label")
    table.add_column("Hours")
    table.add_column("Milestones")
    for row in rows:
        hours = f"{row['completed_hours']:.2f}/{row['total_hours']:.2f}"
        counts = f"{row['completed_count']}/{row['total_count']}"
        table.add_row(row["created_at"], row["label"], hours, counts)
    rprint(table)


app.add_typer(progress_app, name="progress")
app.add_typer(service_app, name="service")


@service_app.command("start")
def service_start() -> None:
    """Start the background Milstone web service on port 8123."""

    # Check if already running
    if _ping_server(MILSTONE_SERVER_PORT):
        typer.echo(f"Milstone web service is already running on port {MILSTONE_SERVER_PORT}.")
        return

    # Start the server
    try:
        _get_or_start_server()
        typer.echo(f"Milstone web service started on port {MILSTONE_SERVER_PORT}.")
        log_path = _server_log_path()
        typer.echo(f"Server logs: {log_path}")
    except typer.BadParameter:
        raise


@service_app.command("stop")
def service_stop() -> None:
    """Stop the background Milstone web service."""

    if not _ping_server(MILSTONE_SERVER_PORT):
        typer.echo("Milstone web service is not running.")
        return

    typer.echo(f"Stopping Milstone web service on port {MILSTONE_SERVER_PORT}...")
    graceful = _shutdown_service()
    typer.echo("Service stopped cleanly." if graceful else "Service stopped.")


@service_app.command("restart")
def service_restart() -> None:
    """Restart the background Milstone web service."""

    typer.echo("Restarting Milstone web service...")
    _shutdown_service()
    _get_or_start_server()
    typer.echo(f"Milstone web service restarted on port {MILSTONE_SERVER_PORT}.")


@service_app.command("status")
def service_status() -> None:
    """Check the status of the Milstone web service."""

    if _ping_server(MILSTONE_SERVER_PORT):
        typer.echo(f"Milstone web service is running on port {MILSTONE_SERVER_PORT}.")
        typer.echo(f"Access the dashboard at: http://127.0.0.1:{MILSTONE_SERVER_PORT}")
    else:
        typer.echo("Milstone web service is not running.")

    log_path = _server_log_path()
    typer.echo(f"Server logs: {log_path}")


@service_app.command("logs")
def service_logs() -> None:
    """Show the location of the server log file."""

    log_path = _server_log_path()
    typer.echo(f"Server log file: {log_path}")

    if log_path.exists():
        size_kb = log_path.stat().st_size / 1024
        typer.echo(f"Log file size: {size_kb:.2f} KB")
    else:
        typer.echo("Log file does not exist yet (server has not been started).")


@project_app.command("ui")
def project_ui(
    path: Path = typer.Option(Path("."), "--path", help="Project root"),
) -> None:
    """Start (or reuse) the Milstone web server and open the UI for the requested project."""

    project_root = path.resolve()
    state_dir = _state_dir(project_root)
    if not state_dir.exists():
        raise typer.BadParameter("Missing .milstone state. Run `milstone project init` first.")

    conn = _connect_existing(project_root)
    try:
        project_id = _resolve_project_fields(conn, None, None, create_if_missing=False)
        project_info = _fetch_project_info(conn, project_id)
    finally:
        conn.close()

    project_entry = {
        "key": project_info["key"],
        "name": project_info["name"],
        "description": project_info["description"],
        "path": str(project_root),
    }
    _record_project_history(state_dir, project_entry)

    # Get or start server on hardcoded port
    _get_or_start_server()

    _register_project_with_server(MILSTONE_SERVER_PORT, project_info, project_root, state_dir)
    url = f"http://127.0.0.1:{MILSTONE_SERVER_PORT}/?project={project_info['key']}"
    typer.echo(f"Opening Milstone web UI at {url}")
    # Launch browser without waiting for it to exit
    typer.launch(url, wait=False)


if __name__ == "__main__":
    app()
def _node_sort_key(node: dict) -> tuple:
    priority = node.get("priority")
    due = node.get("dueDate") or "9999-12-31"
    title = (node.get("title") or "").lower()
    return (priority if priority is not None else 999, due, title)


def _build_milestone_forest(conn: sqlite3.Connection, project_id: int, since: Optional[datetime]) -> List[dict]:
    rows = conn.execute(
        """
        SELECT id, parent_id, slug, title, description, status, priority, owner,
               start_date, due_date, completed_at, expected_hours, deleted, created_at
        FROM milestones WHERE project_id = ?
        """,
        (project_id,),
    ).fetchall()
    filtered = [row for row in rows if not row["deleted"] and _milestone_in_period(row, since)]
    node_map: Dict[int, dict] = {}
    roots: List[dict] = []
    for row in filtered:
        node = {
            "id": row["id"],
            "parentId": row["parent_id"],
            "slug": row["slug"],
            "title": row["title"],
            "description": (row["description"] or "").strip(),
            "status": _canonical_status(row["status"]),
            "priority": row["priority"],
            "owner": row["owner"],
            "startDate": row["start_date"],
            "dueDate": row["due_date"],
            "expectedHours": float(row["expected_hours"] or 0.0),
            "completedAt": row["completed_at"],
            "createdAt": row["created_at"],
            "children": [],
        }
        node_map[row["id"]] = node

    for node in node_map.values():
        parent_id = node["parentId"]
        if parent_id and parent_id in node_map:
            node_map[parent_id]["children"].append(node)
        else:
            roots.append(node)

    def _sort_children(node: dict) -> None:
        node["children"].sort(key=_node_sort_key)
        for child in node["children"]:
            _sort_children(child)

    def _compute_total_hours(node: dict) -> float:
        total = float(node["expectedHours"])
        for child in node["children"]:
            total += _compute_total_hours(child)
        node["totalHours"] = total
        return total

    for root in roots:
        _sort_children(root)
        _compute_total_hours(root)
    roots.sort(key=_node_sort_key)
    return roots


def _collect_active_nodes(roots: List[dict]) -> List[dict]:
    def visit(node: dict) -> List[dict]:
        collected: List[dict] = []
        for child in node["children"]:
            collected.extend(visit(child))
        is_active = node["status"] != "done"
        if is_active:
            clone = {
                key: node[key]
                for key in (
                    "slug",
                    "title",
                    "description",
                    "status",
                    "priority",
                    "owner",
                    "dueDate",
                    "expectedHours",
                    "totalHours",
                )
            }
            clone["children"] = collected
            return [clone]
        return collected

    result: List[dict] = []
    for root in roots:
        result.extend(visit(root))
    return result


def _collect_completed_nodes(roots: List[dict]) -> List[dict]:
    gathered: List[dict] = []

    def flatten(node: dict) -> None:
        gathered.append(node)
        for child in node["children"]:
            flatten(child)

    for root in roots:
        flatten(root)

    completed = [node for node in gathered if node["status"] == "done"]

    def sort_key(node: dict):
        dt = _parse_datetime(node["completedAt"])
        return (dt or datetime.min.replace(tzinfo=timezone.utc), node["title"].lower())

    completed.sort(key=sort_key, reverse=True)
    return completed


def _format_datetime_label(value: Optional[str]) -> str:
    if not value:
        return "Not recorded"
    dt = _parse_datetime(value)
    if not dt:
        return value
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def _render_report_markdown(
    project: Dict[str, Optional[str]],
    progress: Dict[str, Dict[str, float]],
    since: Optional[datetime],
    active_nodes: List[dict],
    completed_nodes: List[dict],
) -> str:
    lines: List[str] = []
    generated_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M %Z")
    lines.append("# Milstone Status Report")
    lines.append(f"_Generated automatically by Milstone on {generated_ts}_")
    lines.append("")
    lines.append(f"**Project:** {project.get('name')}")
    if project.get("description"):
        lines.append(f"**Description:** {project['description']}")
    tracking_label = _format_datetime_label(since.isoformat()) if since else "Project start"
    stats = progress.get("stats", {})
    total_hours = stats.get("totalHours", 0.0)
    completed_hours = stats.get("completedHours", 0.0)
    remaining_hours = max(total_hours - completed_hours, 0.0)
    total_count = stats.get("totalCount", 0)
    completed_count = stats.get("completedCount", 0)

    lines.append("")
    lines.append("## Progress Overview")
    lines.append(f"- Tracking since: {tracking_label}")
    lines.append(f"- Completed hours: {completed_hours:.2f}h")
    lines.append(f"- Remaining hours: {remaining_hours:.2f}h")
    lines.append(f"- Completed milestones: {completed_count}/{total_count}")

    lines.append("")
    lines.append("## Active Milestones")
    if not active_nodes:
        lines.append("_No active milestones at this time._")
    else:
        for node in active_nodes:
            _render_active_node(lines, node, depth=0)

    lines.append("")
    lines.append("## Completed Milestones")
    if not completed_nodes:
        lines.append("_No milestones marked as done in this period._")
    else:
        for node in completed_nodes:
            completed_label = _format_datetime_label(node.get("completedAt"))
            lines.append(
                f"- **{node['title']}** (`{node['slug']}`) — completed {completed_label} — {node['expectedHours']:.2f}h"
            )
            if node.get("description"):
                lines.append(f"  - {node['description']}")

    return "\n".join(lines) + "\n"


def _render_active_node(lines: List[str], node: dict, depth: int) -> None:
    indent = "  " * depth
    parts: List[str] = [f"**{node['title']}** (`{node['slug']}`)"]
    parts.append(f"status: {node['status']}")
    if node.get("owner"):
        parts.append(f"owner: {node['owner']}")
    if node.get("dueDate"):
        parts.append(f"due: {node['dueDate']}")
    parts.append(
        f"hours: {node['expectedHours']:.2f} / {node['totalHours']:.2f}"
    )
    if node.get("description"):
        parts.append(f"desc: {node['description'].replace('\n', ' ')}")
    lines.append(f"{indent}- " + " | ".join(parts))
    for child in node.get("children", []):
        _render_active_node(lines, child, depth + 1)
