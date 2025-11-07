# Milstone

Milstone is a CLI-first milestone tracking tool that keeps all operational state inside a project-local `.milstone/` directory (SQLite database, LLM usage guide, generated assets) while still emitting a Markdown snapshot of active milestones for easy sharing.

## Getting Started

```bash
pip install -e .
milstone project init "My Project" .
```

`milstone project init` seeds the default project row (`--project-key` defaults to `default`). All generated artifacts live under `.milstone/`. The bundled Flask app serves both the HTML dashboard and the JSON APIs the UI depends on—no external Node tooling required.

## CLI Commands

- `milstone milestone add "Feature A"` – create a milestone by title; the tool auto-generates a stable slug and accepts options such as `--status`, `--priority`, `--owner`, `--project-key` (defaults to `default`), `--parent`, and `--expected-hours` (default `1`).
- `milstone milestone update <slug>` – patch existing milestones. You can re-parent (`--parent`, `--clear-parent`), tweak expected hours, and soft delete / restore (`--deleted/--undeleted`) without dropping historical data.
- `milstone milestone list` – render a Rich-powered tree view for the selected project. Combine filters like `--status`, `--exclude-done`, or `--include-deleted`; by default it focuses on milestones active since the last progress reset.
- `milstone project report` – render `milstone_status.md` in the current working directory unless `--output` is provided. This is the only artifact written outside `.milstone/`.
- `milstone progress show` – display the current period’s progress (`completed_hours / total_hours`, milestone counts) since the most recent reset.
- `milstone progress reset` – capture the current stats as a snapshot (optionally naming it) and start a fresh tracking period.
- `milstone progress history` – list saved snapshots so you can review earlier periods at a glance.
- `milstone service stop` – shut down the background web service without opening the UI (gracefully via `/__stop`, SIGTERM fallback).
- `milstone project ui` – spins up (or reuses) the Flask web server, then opens the embedded dashboard (no Node/Next.js runtime needed). Make sure `pip install -e .` (or `pip install flask`) has been run so the CLI’s interpreter has Flask available.
- `milstone service start|stop|restart` – manage the long-running web service without opening the UI (gracefully via `/__stop`, SIGTERM fallback).

All commands accept `--path` to point at another project root that already contains a `.milstone/` directory. The `milstone project ui` command launches the Flask UI automatically, so simply run it and your browser will open to the dashboard.

## Hierarchical Milestones & Soft Deletes

- Every milestone can reference a parent milestone, allowing you to break large goals into nested sub-milestones. Both the CLI (`milstone milestone list`) and the web UI visualize this structure as a tree.
- Milestones carry an `expected_hours` estimate (default `1`). Progress is calculated as `completed_hours / total_hours` for the active tracking period (since the last reset), so longer efforts count proportionally.
- Milestones now have a `deleted` flag instead of being removed from the database. Use `milstone milestone update <slug> --deleted` (or the web “Soft Delete” form) to hide a milestone while retaining full history.
- Resetting progress stores a snapshot (`milstone progress reset`) that preserves aggregate hours/counts for historical reference before starting the next period.
- The browser UI mirrors popular React/Next.js dashboards: it shows recent projects, lets you toggle deleted milestones, edit/create/delete milestones inline, reset progress, review snapshot history, and keeps the tree + progress card in sync with CLI actions via the shared SQLite state.
