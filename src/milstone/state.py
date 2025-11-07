"""Shared state helpers for the Milstone CLI and web server."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

WEB_HISTORY_FILENAME = "web_history.json"
SERVER_INFO_FILENAME = "server_info.json"
GLOBAL_STATE_ROOT = Path.home() / ".milstone-server"


def _history_path(state_dir: Path) -> Path:
    return state_dir / WEB_HISTORY_FILENAME


def _server_info_path(state_dir: Path) -> Path:
    return state_dir / SERVER_INFO_FILENAME


def load_history(state_dir: Path) -> Dict[str, Any]:
    path = _history_path(state_dir)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"projects": [], "current_project": None, "last_opened_at": None}


def save_history(state_dir: Path, history: Dict[str, Any]) -> None:
    path = _history_path(state_dir)
    path.write_text(json.dumps(history, indent=2), encoding="utf-8")


def record_project_open(state_dir: Path, entry: Dict[str, Any]) -> Dict[str, Any]:
    history = load_history(state_dir)
    now = datetime.now(timezone.utc).isoformat()
    entry_with_ts = {**entry, "last_opened": now}
    projects: List[Dict[str, Any]] = history.get("projects", [])
    updated = False
    for idx, project in enumerate(projects):
        if project.get("key") == entry["key"]:
            projects[idx] = {**project, **entry_with_ts}
            updated = True
            break
    if not updated:
        projects.append(entry_with_ts)
    projects.sort(key=lambda item: item.get("last_opened", ""), reverse=True)
    history["projects"] = projects
    history["current_project"] = entry["key"]
    history["last_opened_at"] = now
    save_history(state_dir, history)
    return history


def read_server_info(state_dir: Path) -> Optional[Dict[str, Any]]:
    path = _server_info_path(state_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_server_info(state_dir: Path, info: Dict[str, Any]) -> None:
    path = _server_info_path(state_dir)
    path.write_text(json.dumps(info, indent=2), encoding="utf-8")


def clear_server_info(state_dir: Path) -> None:
    path = _server_info_path(state_dir)
    if path.exists():
        path.unlink()


def _global_root() -> Path:
    GLOBAL_STATE_ROOT.mkdir(parents=True, exist_ok=True)
    return GLOBAL_STATE_ROOT


def global_runtime_dir() -> Path:
    return _global_root()


def read_global_server_info() -> Optional[Dict[str, Any]]:
    path = _global_root() / SERVER_INFO_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_global_server_info(info: Dict[str, Any]) -> None:
    path = _global_root() / SERVER_INFO_FILENAME
    path.write_text(json.dumps(info, indent=2), encoding="utf-8")


def clear_global_server_info() -> None:
    path = _global_root() / SERVER_INFO_FILENAME
    if path.exists():
        path.unlink()
