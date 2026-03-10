---
name: sync-linear
description: Use when syncing Claude Code sessions to Linear issues. Triggered by cron or manually via /sync-linear. Reads local session JSONL files, matches to existing Linear issues, creates or updates issues with summaries and todos.
---

# Sync Claude Sessions to Linear

## Overview

Syncs Claude Code session data to Linear issues. Sessions and issues have a **many-to-many** relationship: one session can touch multiple issues, multiple sessions can contribute to one issue. Designed for team collaboration — each member runs their own sync against a shared Linear project.

## Prerequisites

- Linear MCP server connected (for `list_issues`, `save_issue`, `get_issue`)
- Python 3.10+ available
- Helper scripts at `./scripts/` (relative to this skill's directory)
- Config at `~/.claude-linear-sync/config.yaml`
- **SKILL_DIR**: `~/.claude/skills/sync-linear` — all `./` paths below are relative to this directory

## Workflow

**Follow these steps in order.**

### Step 1: Extract session data

Run the Python extractor to get compact session summaries:

```bash
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../" && pwd)" # resolves to ~/.claude/skills/sync-linear
python3 "$SKILL_DIR/scripts/extract_sessions.py" --config ~/.claude-linear-sync/config.yaml --state ~/.claude-linear-sync/state.json
```

This outputs JSON to stdout with:
- `config.linear_team` — target Linear team name
- `config.user_email` — Linear user to assign new issues to
- `sessions[]` — array of session summaries (id, slug, branch, cwd, user messages, tokens, timestamps, linked issues)

If `sessions` is empty, output "No sessions to sync" and stop.

### Step 2: Fetch existing Linear issues

Use the Linear MCP `list_issues` tool with the `team` parameter set to `config.linear_team`. You need issue titles, descriptions, and identifiers for matching.

### Step 3: Match sessions to issues

For each session, analyze the `user_messages` to identify **topics/tasks worked on**. A single session may have multiple topics.

For each topic:
1. Compare against existing Linear issue titles and descriptions
2. If content matches an existing issue — plan to **update** it
3. If no match — plan to **create** a new issue

Also check `existing_linear_issue_ids` from the session data — these are known matches from previous syncs.

### Step 4: Create or update Linear issues

**For new issues**, use `save_issue` with:
- **Title**: Short topic-based title (what was worked on)
- **Description** using this format:

```markdown
## Summary
[2-3 sentence summary written for team context — useful to someone NOT in the session]

## Todos
- [ ] [todos extracted from conversation text]

## Contributing Sessions
- @[user_email] ([date]) [PR #N](pr_url) — if session has a pr_url, include the linked PR; omit otherwise: [1-sentence summary of what this session specifically worked on]

## Metrics
- Tokens: [total_input + total_output]
- Last synced: [now]
```

- **Assignee**: Set to `user_email` from config
- **Team**: Set to `linear_team` from config
**For existing issues**, use `get_issue` to read current description, then `save_issue` to:
- Merge the summary (update with new context, don't replace)
- Append new todos
- Add the new session to Contributing Sessions
- Update metrics

### Step 4b: Tag Linear issues to PRs

After creating/updating issues, for each unique PR found in the sessions, add the Linear issue identifier to the PR body so Linear's GitHub integration auto-syncs status (e.g. PR merge → issue Done).

For each session that has `pr_url` and `pr_number`:
1. Read the current PR body: `gh pr view <pr_number> --json body --jq .body` (run in the session's `cwd`)
2. If the Linear issue identifier (e.g. `CORE-243`) is NOT already in the body, append it:
   ```bash
   gh pr edit <pr_number> --body "$(existing_body)\n\nLinear: CORE-243"
   ```
3. Skip if the identifier is already present (avoid duplicates)

If multiple Linear issues map to the same PR, append all of them.

### Step 5: Persist sync state

Build a sync results JSON and pipe it to the state updater:

```json
{
  "synced": [
    {
      "session_id": "uuid",
      "file_size": 45678,
      "action": "created",
      "linear_issue_ids": ["PROJ-123"]
    }
  ]
}
```

Write this to a temp file, then run:

```bash
cat /tmp/sync_results.json | python3 "$SKILL_DIR/scripts/update_state.py" --state ~/.claude-linear-sync/state.json
```

### Step 6: Report results

Output a summary of what was synced:
- How many sessions processed
- Issues created (with identifiers)
- Issues updated (with identifiers)
- Any errors encountered

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Replacing issue description instead of merging | Always read existing description first, then merge |
| Creating duplicate issues for same topic | Check existing issues AND `existing_linear_issue_ids` from state |
| Assigning to wrong user | Use `user_email` from config, not hardcoded |
| Summarizing too verbosely | Keep summaries to 2-3 sentences, written for teammates |
