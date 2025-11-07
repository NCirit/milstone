# Milstone Database Schema

Milstone uses SQLite for local-first milestone management. Tables are normalized so CLI and future web UI share the same source of truth.

## Tables

### projects
- `id` INTEGER PK
- `key` TEXT unique short code (e.g., "core")
- `name` TEXT descriptive project title
- `description` TEXT optional
- `created_at` TEXT ISO timestamp (defaults to `CURRENT_TIMESTAMP`)

### milestones
- `id` INTEGER PK
- `project_id` INTEGER FK → projects.id
- `slug` TEXT unique within a project (URL-friendly)
- `title` TEXT human-readable title
- `description` TEXT markdown/plaintext
- `status` TEXT (`active`, `blocked`, `on_hold`, `done`, etc.)
- `priority` INTEGER (1 highest)
- `owner` TEXT (single point of contact)
- `start_date` TEXT ISO date
- `due_date` TEXT ISO date
- `completed_at` TEXT ISO timestamp
- `parent_id` INTEGER nullable self-reference → milestones.id (supports nested milestone trees)
- `deleted` INTEGER flag (0 = active, 1 = soft deleted)
- `expected_hours` REAL (defaults to `1`, fuels progress calculations)
- `created_at` TEXT ISO timestamp
- `updated_at` TEXT ISO timestamp (use trigger later)
- UNIQUE(project_id, slug)

### progress_snapshots
- `id` INTEGER PK
- `project_id` INTEGER FK → projects.id
- `label` TEXT description when reset occurred
- `created_at` TEXT ISO timestamp
- `total_hours` REAL
- `completed_hours` REAL
- `total_count` INTEGER
- `completed_count` INTEGER

### milestone_updates
- `id` INTEGER PK
- `milestone_id` INTEGER FK → milestones.id
- `author` TEXT
- `summary` TEXT required progress note
- `status` TEXT optional override snapshot
- `progress` INTEGER 0-100 optional
- `created_at` TEXT ISO timestamp

### milestone_dependencies
- `id` INTEGER PK
- `milestone_id` INTEGER FK → milestones.id (the dependent)
- `depends_on_id` INTEGER FK → milestones.id (the blocker)
- `relation` TEXT (defaults to `blocks`, can be `relates-to`)
- UNIQUE(milestone_id, depends_on_id)

### tags
- `id` INTEGER PK
- `name` TEXT unique tag label

### milestone_tags
- `milestone_id` INTEGER FK → milestones.id
- `tag_id` INTEGER FK → tags.id
- PRIMARY KEY (milestone_id, tag_id)

### audit_log
- `id` INTEGER PK
- `entity_type` TEXT (e.g., `milestone`)
- `entity_id` INTEGER referencing the entity row
- `action` TEXT (`create`, `update`, `delete`...)
- `payload` TEXT JSON blob storing diffs/context
- `created_at` TEXT ISO timestamp

## Notes
- Enable WAL mode for better concurrent CLI/web access.
- Triggers should later maintain `milestones.updated_at` and append audit_log rows.
- Future web UI can translate these tables into GraphQL/REST responses without extra migrations.
- Tree views in both CLI and web UI are derived from the `(project_id, parent_id)` relationships; soft deletes ensure history is preserved even when milestones fall out of scope.
- Progress snapshots capture milestone statistics at reset time (e.g., `milstone progress reset`). Resets store the current totals and start a new active period without duplicating milestone records.
