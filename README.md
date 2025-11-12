# Milstone

Milstone is a CLI-first milestone tracking tool with a built-in web dashboard. It keeps all state inside a project-local `.milstone/` directory (SQLite database, LLM usage guide) while providing both command-line and web interfaces for managing milestones, tracking progress, and viewing project history.

## Features

- **Hierarchical Milestones**: Create nested milestone structures with parent-child relationships
- **Progress Tracking**: Track hours and completion rates with periodic snapshots
- **Live Web Dashboard**: Real-time updates with notifications for changes
- **LLM-Friendly**: Includes auto-generated instructions for language models
- **Multi-Project Support**: Recent projects sidebar with automatic tracking
- **Soft Deletes**: Retain historical data while hiding completed or obsolete milestones

## Installation

### Install from Git (SSH)

```bash
pip install git+ssh://git@github.com/ncirit/milstone.git
```

### Install from Git (HTTPS)

```bash
pip install git+https://github.com/ncirit/milstone.git
```

### Install for Development

```bash
git clone git@github.com:ncirit/milstone.git
cd milstone
pip install -e .
```

## Quick Start

### 1. Initialize a Project

```bash
cd /path/to/your/project
milstone project init "My Project"
```

This creates a `.milstone/` directory containing:
- `milstone.db` - SQLite database with all milestone data
- `llm_instructions.txt` - Auto-generated guide for LLM usage (customizable)

### 2. Create Milestones

```bash
# Create a top-level milestone
milstone milestone add "Setup Backend Infrastructure" --expected-hours 8

# Create a child milestone
milstone milestone add "Setup Database" --parent setup-backend-infrastructure --expected-hours 3

# Add a log entry
milstone log add setup-database "Created schema and migrations"
```

### 3. Launch the Web UI

```bash
milstone project ui
```

This starts the web server on port 8123 and opens your browser. The dashboard provides:
- Interactive milestone tree with expand/collapse
- Real-time recent changes feed with notifications
- Progress tracking and history
- Create, edit, and delete milestones
- Add log entries and track status changes

### 4. Generate Status Report

```bash
milstone project report
```

Creates `milstone_status.md` with the current milestone status.

## Example Workflow

```bash
# Start a new project
milstone project init "E-commerce Platform"

# Add main features
milstone milestone add "User Authentication" --expected-hours 16
milstone milestone add "Product Catalog" --expected-hours 24
milstone milestone add "Shopping Cart" --expected-hours 20

# Break down into tasks
milstone milestone add "Login System" --parent user-authentication --expected-hours 8
milstone milestone add "OAuth Integration" --parent user-authentication --expected-hours 8

# Track progress
milstone log add login-system "Implemented JWT authentication"
milstone milestone update login-system --status done

# View progress
milstone progress show

# Open web dashboard for visual management
milstone project ui

# Save a snapshot at milestone completion
milstone progress reset --label "Phase 1 Complete"
```

## Service Management

```bash
# Start the web service in background
milstone service start

# Check service status
milstone service status

# View server logs
milstone service logs

# Stop the service
milstone service stop
```

## LLM Instructions

Milstone includes auto-generated instructions for LLM models in `.milstone/llm_instructions.txt`. This file helps language models understand how to:
- Use the CLI commands correctly
- Manage milestones and track progress
- Follow project-specific conventions

### Customizing LLM Instructions

After running `milstone project init`, you can customize the "User Instructions" section in `.milstone/llm_instructions.txt` to add project-specific guidelines:

```txt
User Instructions
-----------------
* Write detailed descriptions for each milestone explaining what needs to be done.
* Only mark milestones as completed when all work is finished.
* Split large milestones into smaller ones that can be completed quickly.
* Use `milstone log add` to record progress updates regularly.
* Review milestone status frequently to keep tracking accurate.
```

### Providing Instructions to LLMs

When working with language models (like Claude, GPT-4, etc.), you can:

1. **Include the file in context**: Share the contents of `.milstone/llm_instructions.txt` with the LLM
2. **Reference in prompts**: "Please follow the guidelines in .milstone/llm_instructions.txt"
3. **Project initialization**: The LLM can run `milstone project init` and read the generated instructions

Example prompt:
```
I'm working on a project tracked with Milstone. Please read .milstone/llm_instructions.txt
and help me create milestones for implementing a REST API with authentication, database
integration, and documentation.
```

The LLM will then use the commands appropriately and follow your project's conventions.

## CLI Help

All commands support `--help` for detailed usage:

```bash
milstone --help
milstone milestone --help
milstone milestone add --help
```

## Project Structure

```
your-project/
├── .milstone/
│   ├── milstone.db           # SQLite database
│   └── llm_instructions.txt  # LLM usage guide (customizable)
├── milstone_status.md        # Generated status report
└── ... your project files ...
```

## Architecture

- **CLI**: Python with Typer for command-line interface
- **Database**: SQLite for local storage (inside `.milstone/`)
- **Web Server**: Flask serving both HTML dashboard and JSON APIs
- **Frontend**: Vanilla JavaScript with real-time polling for updates
- **Global State**: Server info and project history in `~/.milstone-server/`

## License

MIT
