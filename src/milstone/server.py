"""Flask application serving Milstone UI + JSON APIs."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import time

from flask import Flask, jsonify, render_template, request

from . import state

BASE_DIR = Path(__file__).resolve().parent
STATIC_FOLDER = BASE_DIR / "static"
TEMPLATE_FOLDER = BASE_DIR / "templates"
DB_FILENAME = "milstone.db"
DEFAULT_EXPECTED_HOURS = 1.0
# Project registry is now handled by state.load_history() / state.save_history()
# No need for separate in-memory registry

app = Flask(
    __name__,
    static_folder=str(STATIC_FOLDER),
    template_folder=str(TEMPLATE_FOLDER),
)


# ---------------------------------------------------------------------------
# Project history helpers (uses state.py functions)
# ---------------------------------------------------------------------------

def _get_project_entry(project_key: str) -> Dict[str, Any]:
    """Get project entry by key from history."""
    history = state.load_history()
    projects = history.get("projects", [])
    for project in projects:
        if project.get("key") == project_key:
            return project
    raise KeyError(f"Project '{project_key}' not found in history")




# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db_path(state_dir: Path) -> Path:
    return state_dir / DB_FILENAME


def _connect(state_dir: Path) -> sqlite3.Connection:
    db_path = _db_path(state_dir)
    if not db_path.exists():
        raise FileNotFoundError(f"Missing database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_schema(conn)
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _maybe_add_column(conn: sqlite3.Connection, table: str, column: str, column_sql: str) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql}")
        conn.commit()


def _normalize_statuses(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("UPDATE milestones SET status = 'active' WHERE status = 'planned'")


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
            print(f"Migrated project key from '{old_key}' to '{new_key}'")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
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
        """
    )
    _maybe_add_column(conn, "milestones", "parent_id", "parent_id INTEGER REFERENCES milestones(id) ON DELETE SET NULL")
    _maybe_add_column(conn, "milestones", "deleted", "deleted INTEGER NOT NULL DEFAULT 0")
    _maybe_add_column(conn, "milestones", "expected_hours", "expected_hours REAL NOT NULL DEFAULT 1")
    _maybe_add_column(conn, "milestone_updates", "sequence", "sequence INTEGER")
    _ensure_log_sequences(conn)
    _normalize_statuses(conn)
    _migrate_old_project_keys(conn)


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


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


def _milestone_in_period(row: sqlite3.Row, since: Optional[datetime]) -> bool:
    if since is None:
        return True
    start, end = _milestone_window(row)
    now_dt = datetime.now(timezone.utc)
    effective_end = end or now_dt
    effective_start = start or effective_end
    return effective_end >= since and effective_start <= now_dt


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


def _slugify(title: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "milestone"


def _generate_slug(conn: sqlite3.Connection, project_id: int, title: str) -> str:
    base = _slugify(title)
    slug = base
    counter = 2
    while conn.execute("SELECT 1 FROM milestones WHERE project_id = ? AND slug = ?", (project_id, slug)).fetchone():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def _canonical_status(value: Optional[str]) -> str:
    if not value:
        return "active"
    value = value.strip().lower()
    if value == "planned":
        return "active"
    return value


def _auto_completed_at(status: str, existing: Optional[str]) -> Optional[str]:
    status = _canonical_status(status)
    if status == "done":
        return datetime.now(timezone.utc).isoformat()
    return None


def _milestone_by_slug(conn: sqlite3.Connection, project_id: int, slug: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM milestones WHERE project_id = ? AND slug = ?",
        (project_id, slug),
    ).fetchone()
    if row is None:
        raise ValueError(f"Milestone '{slug}' not found")
    return row


def _project_row(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE key = ?", (key,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO projects (key, name, description) VALUES (?, ?, ?)",
            (key, key, None),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM projects WHERE key = ?", (key,)).fetchone()
    return row


def _record_snapshot(conn: sqlite3.Connection, project_id: int, label: Optional[str]) -> sqlite3.Row:
    since = _current_period_start(conn, project_id)
    stats = _progress_stats(conn, project_id, since)
    with conn:
        cursor = conn.execute(
            "INSERT INTO progress_snapshots (project_id, label, total_hours, completed_hours, total_count, completed_count)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                project_id,
                label or f"Reset {_today_iso()}",
                stats["stats"]["totalHours"],
                stats["stats"]["completedHours"],
                stats["stats"]["totalCount"],
                stats["stats"]["completedCount"],
            ),
        )
    return conn.execute("SELECT * FROM progress_snapshots WHERE id = ?", (cursor.lastrowid,)).fetchone()


def _snapshot_history(conn: sqlite3.Connection, project_id: int) -> List[dict]:
    rows = conn.execute(
        "SELECT label, created_at, total_hours, completed_hours, total_count, completed_count "
        "FROM progress_snapshots WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()
    return [
        {
            "label": row["label"],
            "createdAt": row["created_at"],
            "totalHours": row["total_hours"],
            "completedHours": row["completed_hours"],
            "totalCount": row["total_count"],
            "completedCount": row["completed_count"],
        }
        for row in rows
    ]


def _progress_stats(conn: sqlite3.Connection, project_id: int, since: Optional[datetime]) -> dict:
    rows = conn.execute(
        "SELECT expected_hours, status, start_date, due_date, completed_at, created_at, deleted "
        "FROM milestones WHERE project_id = ?",
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
        "since": since.isoformat() if since else None,
        "stats": {
            "totalHours": total_hours,
            "completedHours": completed_hours,
            "totalCount": total_count,
            "completedCount": completed_count,
            "ratio": ratio,
        },
    }


def _list_milestones(conn: sqlite3.Connection, project_id: int, include_deleted: bool) -> List[dict]:
    since = _current_period_start(conn, project_id)
    rows = conn.execute(
        "SELECT id, parent_id, slug, title, description, status, priority, owner, start_date, due_date, completed_at, deleted, expected_hours, created_at "
        "FROM milestones WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    if not include_deleted:
        rows = [row for row in rows if not row["deleted"]]
    rows = [row for row in rows if _milestone_in_period(row, since)]
    rows.sort(key=lambda r: (r["priority"], r["due_date"] or "9999-12-31"))
    tree, node_map = _rows_to_tree(rows)
    _attach_logs(conn, node_map)
    return tree


def _rows_to_tree(rows: List[sqlite3.Row]) -> tuple[List[dict], Dict[int, dict]]:
    node_map: Dict[int, dict] = {}
    order: List[int] = []
    for row in rows:
        node = {
            "id": row["id"],
            "parentId": row["parent_id"],
            "slug": row["slug"],
            "title": row["title"],
            "description": row["description"],
            "status": "deleted" if row["deleted"] else row["status"],
            "priority": row["priority"],
            "owner": row["owner"],
            "startDate": row["start_date"],
            "dueDate": row["due_date"],
            "expectedHours": row["expected_hours"],
            "deleted": bool(row["deleted"]),
            "children": [],
            "logs": [],
        }
        node_map[row["id"]] = node
        order.append(row["id"])

    roots: List[dict] = []
    for node_id in order:
        node = node_map[node_id]
        parent_id = node["parentId"]
        if parent_id and parent_id in node_map:
            node_map[parent_id]["children"].append(node)
        else:
            roots.append(node)
    roots = roots or list(node_map.values())
    return roots, node_map


def _attach_logs(conn: sqlite3.Connection, node_map: Dict[int, dict]) -> None:
    if not node_map:
        return
    milestone_ids = list(node_map.keys())
    placeholders = ",".join("?" for _ in milestone_ids)
    rows = conn.execute(
        f"SELECT id, milestone_id, sequence, author, summary, status, progress, created_at "
        f"FROM milestone_updates WHERE milestone_id IN ({placeholders}) ORDER BY milestone_id, sequence",
        milestone_ids,
    ).fetchall()
    for row in rows:
        log = _log_row_to_dict(row)
        node_map[row["milestone_id"]]["logs"].append(log)


def _create_milestone(conn: sqlite3.Connection, project_id: int, payload: dict) -> str:
    slug = _generate_slug(conn, project_id, payload.get("title", ""))
    parent_slug = payload.get("parentSlug")
    parent_id = None
    if parent_slug:
        parent_row = conn.execute(
            "SELECT id FROM milestones WHERE project_id = ? AND slug = ? AND deleted = 0",
            (project_id, parent_slug),
        ).fetchone()
        if parent_row is None:
            raise ValueError(f"Parent milestone '{parent_slug}' not found")
        if parent_slug == slug:
            raise ValueError("A milestone cannot be its own parent")
        parent_id = parent_row["id"]
    status_value = _canonical_status(payload.get("status"))
    completed_at = payload.get("completedAt") or _auto_completed_at(status_value, None)
    with conn:
        conn.execute(
            """
            INSERT INTO milestones (
                project_id, slug, title, description, status, priority, owner,
                start_date, due_date, parent_id, expected_hours, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                slug,
                payload.get("title"),
                payload.get("description"),
                status_value,
                int(payload.get("priority", 3)),
                payload.get("owner"),
                payload.get("startDate"),
                payload.get("dueDate"),
                parent_id,
                float(payload.get("expectedHours", DEFAULT_EXPECTED_HOURS)),
                completed_at,
            ),
        )
    return slug


def _update_milestone(conn: sqlite3.Connection, project_id: int, payload: dict) -> None:
    slug = payload.get("slug")
    if not slug:
        raise ValueError("Missing slug")
    current = conn.execute(
        "SELECT * FROM milestones WHERE project_id = ? AND slug = ?",
        (project_id, slug),
    ).fetchone()
    if current is None:
        raise ValueError(f"Milestone '{slug}' not found")
    updates: Dict[str, object] = {}
    mapping = {
        "title": "title",
        "description": "description",
        "status": "status",
        "priority": "priority",
        "owner": "owner",
        "startDate": "start_date",
        "dueDate": "due_date",
        "completedAt": "completed_at",
    }
    for key, column in mapping.items():
        if payload.get(key) not in (None, ""):
            value = payload[key]
            if key == "status":
                value = _canonical_status(value)
            updates[column] = value
    if payload.get("expectedHours") not in (None, ""):
        updates["expected_hours"] = float(payload["expectedHours"])
    parent_slug = payload.get("parentSlug")
    if parent_slug:
        if parent_slug == slug:
            raise ValueError("A milestone cannot be its own parent")
        parent_row = conn.execute(
            "SELECT id FROM milestones WHERE project_id = ? AND slug = ? AND deleted = 0",
            (project_id, parent_slug),
        ).fetchone()
        if parent_row is None:
            raise ValueError(f"Parent milestone '{parent_slug}' not found")
        updates["parent_id"] = parent_row["id"]
    if payload.get("clearParent"):
        updates["parent_id"] = None
    if payload.get("deleted") is not None:
        updates["deleted"] = 1 if payload["deleted"] else 0
    if "status" in updates:
        status_value = updates["status"]
        if status_value == "done":
            if "completed_at" not in updates or payload.get("completedAt") in (None, ""):
                updates["completed_at"] = _auto_completed_at(status_value, current["completed_at"])
        else:
            updates["completed_at"] = None
    if not updates:
        raise ValueError("No updates specified")
    set_clause = ", ".join(f"{column} = ?" for column in updates)
    values = list(updates.values())
    values.extend([project_id, slug])
    with conn:
        conn.execute(
            f"UPDATE milestones SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE project_id = ? AND slug = ?",
            values,
        )


def _soft_delete_milestone(conn: sqlite3.Connection, project_id: int, slug: str) -> None:
    with conn:
        cursor = conn.execute(
            "UPDATE milestones SET deleted = 1, updated_at = CURRENT_TIMESTAMP WHERE project_id = ? AND slug = ?",
            (project_id, slug),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Milestone '{slug}' not found")


def _reset_project_data(conn: sqlite3.Connection, project_id: int) -> None:
    with conn:
        # remove dependent rows referencing project milestones
        conn.execute(
            "DELETE FROM milestone_updates WHERE milestone_id IN (SELECT id FROM milestones WHERE project_id = ?)",
            (project_id,),
        )
        conn.execute(
            "DELETE FROM milestone_dependencies WHERE milestone_id IN (SELECT id FROM milestones WHERE project_id = ?) "
            "OR depends_on_id IN (SELECT id FROM milestones WHERE project_id = ?)",
            (project_id, project_id),
        )
        conn.execute(
            "DELETE FROM milestone_tags WHERE milestone_id IN (SELECT id FROM milestones WHERE project_id = ?)",
            (project_id,),
        )
        conn.execute("DELETE FROM progress_snapshots WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM milestones WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM milestone_updates WHERE milestone_id NOT IN (SELECT id FROM milestones)")


def _next_log_sequence(conn: sqlite3.Connection, milestone_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS max_seq FROM milestone_updates WHERE milestone_id = ?",
        (milestone_id,),
    ).fetchone()
    return (row["max_seq"] or 0) + 1


def _log_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "sequence": row["sequence"],
        "summary": row["summary"],
        "status": row["status"],
        "progress": row["progress"],
        "author": row["author"],
        "createdAt": row["created_at"],
    }


def _insert_log(conn: sqlite3.Connection, milestone_id: int, payload: dict) -> dict:
    summary = (payload.get("summary") or "").strip()
    if not summary:
        raise ValueError("Summary is required")
    sequence = _next_log_sequence(conn, milestone_id)
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO milestone_updates (milestone_id, summary, sequence)
            VALUES (?, ?, ?)
            """,
            (
                milestone_id,
                summary,
                sequence,
            ),
        )
    row = conn.execute("SELECT * FROM milestone_updates WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _log_row_to_dict(row)


def _update_log(conn: sqlite3.Connection, milestone_id: int, payload: dict) -> dict:
    log_id = payload.get("logId")
    sequence = payload.get("sequence")
    if not log_id and not sequence:
        raise ValueError("Specify logId or sequence to update a log entry")
    row = None
    if log_id:
        row = conn.execute(
            "SELECT * FROM milestone_updates WHERE milestone_id = ? AND id = ?",
            (milestone_id, log_id),
        ).fetchone()
    elif sequence:
        row = conn.execute(
            "SELECT * FROM milestone_updates WHERE milestone_id = ? AND sequence = ?",
            (milestone_id, sequence),
        ).fetchone()
    if row is None:
        raise ValueError("Log entry not found")

    updates: Dict[str, object] = {}
    if payload.get("summary") not in (None, ""):
        updates["summary"] = payload["summary"]
    if not updates:
        raise ValueError("No updates provided")

    set_clause = ", ".join(f"{column} = ?" for column in updates)
    values = list(updates.values())
    values.extend([milestone_id, row["id"]])
    with conn:
        conn.execute(
            f"UPDATE milestone_updates SET {set_clause} WHERE milestone_id = ? AND id = ?",
            values,
        )
    updated = conn.execute("SELECT * FROM milestone_updates WHERE id = ?", (row["id"],)).fetchone()
    return _log_row_to_dict(updated)


def _project_runtime(project_key: str) -> tuple[Dict[str, Any], Path, sqlite3.Connection, sqlite3.Row]:
    entry = _get_project_entry(project_key)
    state_dir = Path(entry["stateDir"]).resolve()
    conn = _connect(state_dir)
    project = _project_row(conn, project_key)
    return entry, state_dir, conn, project


# ---------------------------------------------------------------------------
# Web Views
# ---------------------------------------------------------------------------

@app.get("/")
def home():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/projects")
def api_projects():
    """Get list of all projects from history."""
    history = state.load_history()
    return jsonify(history)


@app.post("/api/projects/register")
def api_register_project():
    payload = request.get_json(force=True)
    project_key = payload.get("projectKey")
    state_dir_raw = payload.get("stateDir")
    if not project_key or not state_dir_raw:
        return ("Missing projectKey or stateDir", 400)

    state_dir = Path(state_dir_raw).resolve()
    if not state_dir.exists():
        return ("stateDir does not exist", 400)
    db_path = _db_path(state_dir)
    if not db_path.exists():
        return ("milstone.db not found in stateDir", 400)

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _connect(state_dir)
        project = _project_row(conn, project_key)
    except Exception as exc:  # pragma: no cover - defensive
        return (f"Failed to validate project: {exc}", 400)
    finally:
        if conn is not None:
            conn.close()

    entry = {
        "key": project_key,
        "name": payload.get("name") or project["name"] or project_key,
        "description": payload.get("description") or project["description"],
        "path": payload.get("path") or state_dir.parent.as_posix(),
        "stateDir": str(state_dir),
    }
    # Record this project in history
    state.record_project_open(entry)
    return jsonify({"status": "ok"})


@app.get("/api/milestones")
def api_milestones():
    project_key = request.args.get("project")
    include_deleted = request.args.get("include_deleted") == "true"
    if not project_key:
        return ("Missing 'project' query parameter", 400)
    try:
        entry, state_dir, conn, project = _project_runtime(project_key)
    except KeyError:
        return ("Project not registered. Run 'milstone project ui' first.", 404)
    except FileNotFoundError as exc:
        return (str(exc), 400)

    try:
        project_id = project["id"]
        milestones = _list_milestones(conn, project_id, include_deleted)
        progress = _progress_stats(conn, project_id, _current_period_start(conn, project_id))
        try:
            history = state.record_project_open(
                {
                    "key": project["key"],
                    "name": project["name"],
                    "description": project["description"],
                    "path": entry.get("path"),
                }
            )
        except Exception:  # pragma: no cover - best-effort persistence
            history = None
        response = {
            "project": {
                "key": project["key"],
                "name": project["name"],
                "description": project["description"],
                "path": entry.get("path"),
            },
            "milestones": milestones,
            "progress": progress,
            "history": history,
        }
    finally:
        conn.close()

    return jsonify(response)


@app.post("/api/milestones/create")
def api_create_milestone():
    project_key = request.args.get("projectKey")
    payload = request.get_json(force=True)
    if not project_key:
        return ("Missing projectKey", 400)
    try:
        _, _, conn, project = _project_runtime(project_key)
    except KeyError:
        return ("Project not registered.", 404)
    except FileNotFoundError as exc:
        return (str(exc), 400)
    try:
        slug = _create_milestone(conn, project["id"], payload)

        return jsonify({"status": "ok", "slug": slug})
    except ValueError as exc:
        return (str(exc), 400)
    finally:
        conn.close()


@app.post("/api/milestones/update")
def api_update_milestone():
    project_key = request.args.get("projectKey")
    payload = request.get_json(force=True)
    if not project_key:
        return ("Missing projectKey", 400)
    try:
        _, _, conn, project = _project_runtime(project_key)
    except KeyError:
        return ("Project not registered.", 404)
    except FileNotFoundError as exc:
        return (str(exc), 400)
    try:
        _update_milestone(conn, project["id"], payload)

        return jsonify({"status": "ok"})
    except ValueError as exc:
        return (str(exc), 400)
    finally:
        conn.close()


@app.post("/api/milestones/delete")
def api_delete_milestone():
    project_key = request.args.get("projectKey")
    payload = request.get_json(force=True)
    if not project_key:
        return ("Missing projectKey", 400)
    try:
        _, _, conn, project = _project_runtime(project_key)
    except KeyError:
        return ("Project not registered.", 404)
    except FileNotFoundError as exc:
        return (str(exc), 400)
    try:
        _soft_delete_milestone(conn, project["id"], payload.get("slug", ""))

        return jsonify({"status": "ok"})
    except ValueError as exc:
        return (str(exc), 400)
    finally:
        conn.close()


@app.post("/api/milestones/logs/create")
def api_create_log():
    project_key = request.args.get("projectKey")
    payload = request.get_json(force=True)
    slug = payload.get("slug")
    if not project_key or not slug:
        return ("Missing projectKey or slug", 400)
    try:
        _, _, conn, project = _project_runtime(project_key)
    except KeyError:
        return ("Project not registered.", 404)
    except FileNotFoundError as exc:
        return (str(exc), 400)
    try:
        milestone = _milestone_by_slug(conn, project["id"], slug)
        log = _insert_log(conn, milestone["id"], payload)

        return jsonify({"status": "ok", "log": log})
    except ValueError as exc:
        return (str(exc), 400)
    finally:
        conn.close()


@app.post("/api/milestones/logs/update")
def api_update_log():
    project_key = request.args.get("projectKey")
    payload = request.get_json(force=True)
    slug = payload.get("slug")
    if not project_key or not slug:
        return ("Missing projectKey or slug", 400)
    try:
        _, _, conn, project = _project_runtime(project_key)
    except KeyError:
        return ("Project not registered.", 404)
    except FileNotFoundError as exc:
        return (str(exc), 400)
    try:
        milestone = _milestone_by_slug(conn, project["id"], slug)
        log = _update_log(conn, milestone["id"], payload)

        return jsonify({"status": "ok", "log": log})
    except ValueError as exc:
        return (str(exc), 400)
    finally:
        conn.close()


@app.post("/api/projects/reset")
def api_reset_project():
    payload = request.get_json(force=True)
    project_key = payload.get("projectKey")
    if not project_key:
        return ("Missing projectKey", 400)
    try:
        _, _, conn, project = _project_runtime(project_key)
    except KeyError:
        return ("Project not registered.", 404)
    except FileNotFoundError as exc:
        return (str(exc), 400)
    try:
        _reset_project_data(conn, project["id"])

        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.get("/api/progress/history")
def api_snapshot_history():
    project_key = request.args.get("project")
    if not project_key:
        return ("Missing 'project' query parameter", 400)
    try:
        _, _, conn, project = _project_runtime(project_key)
    except KeyError:
        return ("Project not registered.", 404)
    except FileNotFoundError as exc:
        return (str(exc), 400)
    try:
        return jsonify(_snapshot_history(conn, project["id"]))
    finally:
        conn.close()


@app.get("/api/recent-changes")
def api_recent_changes():
    project_key = request.args.get("project")
    limit = request.args.get("limit", "20")
    if not project_key:
        return ("Missing 'project' query parameter", 400)
    try:
        limit_int = int(limit)
    except ValueError:
        return ("Invalid limit parameter", 400)
    try:
        _, _, conn, project = _project_runtime(project_key)
    except KeyError:
        return ("Project not registered.", 404)
    except FileNotFoundError as exc:
        return (str(exc), 400)
    try:
        # Combine milestone updates (logs), milestone creations, and a sample of status updates
        # We'll use a UNION to combine different event types
        rows = conn.execute(
            """
            -- Milestone log entries
            SELECT
                'log' as event_type,
                mu.id as event_id,
                mu.summary,
                mu.created_at,
                m.id as milestone_id,
                m.slug,
                m.title
            FROM milestone_updates mu
            JOIN milestones m ON mu.milestone_id = m.id
            WHERE m.project_id = ?

            UNION ALL

            -- Milestone creation events
            SELECT
                'created' as event_type,
                m.id as event_id,
                'Milestone created' as summary,
                m.created_at,
                m.id as milestone_id,
                m.slug,
                m.title
            FROM milestones m
            WHERE m.project_id = ?

            UNION ALL

            -- Milestone status changes (from updated_at being different from created_at)
            SELECT
                'status' as event_type,
                m.id as event_id,
                'Status: ' || m.status as summary,
                m.updated_at as created_at,
                m.id as milestone_id,
                m.slug,
                m.title
            FROM milestones m
            WHERE m.project_id = ? AND m.updated_at != m.created_at

            ORDER BY created_at DESC
            LIMIT ?
            """,
            (project["id"], project["id"], project["id"], limit_int),
        ).fetchall()
        changes = [
            {
                "id": f"{row['event_type']}-{row['event_id']}",  # Unique ID combining type and ID
                "summary": row["summary"],
                "createdAt": row["created_at"],
                "eventType": row["event_type"],
                "milestone": {
                    "id": row["milestone_id"],
                    "slug": row["slug"],
                    "title": row["title"],
                },
            }
            for row in rows
        ]
        return jsonify({"changes": changes})
    finally:
        conn.close()


@app.post("/api/progress/reset")
def api_progress_reset():
    project_key = request.args.get("projectKey")
    payload = request.get_json(force=True)
    if not project_key:
        return ("Missing projectKey", 400)
    try:
        _, _, conn, project = _project_runtime(project_key)
    except KeyError:
        return ("Project not registered.", 404)
    except FileNotFoundError as exc:
        return (str(exc), 400)
    try:
        snapshot = _record_snapshot(conn, project["id"], payload.get("label"))

        return jsonify({
            "status": "ok",
            "snapshot": {
                "label": snapshot["label"],
                "createdAt": snapshot["created_at"],
                "totalHours": snapshot["total_hours"],
                "completedHours": snapshot["completed_hours"],
                "totalCount": snapshot["total_count"],
                "completedCount": snapshot["completed_count"],
            },
        })
    finally:
        conn.close()


@app.post("/__stop")
def shutdown_server() -> dict:
    def _shutdown():
        time.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_shutdown, daemon=True).start()
    return {"status": "stopping"}


@app.get("/__health")
def healthcheck() -> dict:
    return {"status": "ok"}


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Milstone Flask server")
    parser.add_argument("--port", type=int, default=8123, help="Port to bind")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    args = parser.parse_args(argv)

    app.run(host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
