#!/usr/bin/env python3
"""Extract Claude Code session summaries from JSONL files.

Reads ~/.claude/projects/**/*.jsonl, filters by config regex,
extracts compact session summaries as JSON to stdout.
Supports incremental reads (file offset tracking) for re-syncs.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from glob import glob
from pathlib import Path

DEFAULT_CONFIG_DIR = os.path.expanduser("~/.claude-linear-sync")
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_CONFIG_DIR, "config.yaml")
DEFAULT_STATE_PATH = os.path.join(DEFAULT_CONFIG_DIR, "state.json")
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def load_config(config_path: str) -> dict:
    """Load config from YAML file. Falls back to defaults if missing."""
    defaults = {
        "linear": {"default_team": "", "user_email": ""},
        "sessions": {
            "include_patterns": [".*"],
            "exclude_patterns": [],
            "max_message_length": 500,
        },
        "claude": {"model": "sonnet", "max_budget_usd": 1.00},
        "sync": {"max_sessions_per_run": 30, "lookback_hours": 168},
    }
    if not os.path.exists(config_path):
        return defaults
    try:
        # Use PyYAML if available, otherwise parse simple YAML manually
        import yaml
        with open(config_path) as f:
            user_config = yaml.safe_load(f) or {}
        # Merge user config over defaults
        for section, values in user_config.items():
            if section in defaults and isinstance(values, dict):
                defaults[section].update(values)
            else:
                defaults[section] = values
        return defaults
    except ImportError:
        print(
            "Error: PyYAML is required to parse config.yaml. Install it with: pip install pyyaml",
            file=sys.stderr,
        )
        sys.exit(1)


def load_state(state_path: str) -> dict:
    """Load sync state from JSON file."""
    if not os.path.exists(state_path):
        return {"last_run": None, "sessions": {}, "issues": {}}
    try:
        with open(state_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception):
        return {"last_run": None, "sessions": {}, "issues": {}}


def find_active_worktree_dirs(config: dict) -> set[str] | None:
    """Find Claude project dir names for active git worktrees.

    Returns None if worktree filtering is disabled.
    Returns a set of project directory names if enabled.
    """
    if not config["sessions"].get("active_worktrees_only", False):
        return None

    worktree_roots = config["sessions"].get("worktree_roots", [])
    if not worktree_roots:
        print("Warning: active_worktrees_only is true but no worktree_roots configured", file=sys.stderr)
        return set()

    worktree_project_dirs = set()

    for root_path in worktree_roots:
        root_path = os.path.expanduser(root_path)
        if not os.path.isdir(root_path):
            continue

        for dirpath, dirnames, filenames in os.walk(root_path):
            git_path = os.path.join(dirpath, ".git")
            if os.path.isfile(git_path):
                # .git as a file = worktree; convert path to Claude project dir name
                project_dir_name = dirpath.replace("/", "-")
                worktree_project_dirs.add(project_dir_name)
                dirnames.clear()  # Don't descend into worktree subdirs

    return worktree_project_dirs


def find_session_files(config: dict) -> list[str]:
    """Find all JSONL session files, filtered by config patterns and worktree filter."""
    if not os.path.isdir(CLAUDE_PROJECTS_DIR):
        return []

    include_patterns = [re.compile(p) for p in config["sessions"]["include_patterns"]]
    exclude_patterns = [re.compile(p) for p in config["sessions"]["exclude_patterns"]]

    # Build worktree filter (None = disabled, set = only these dirs)
    worktree_dirs = find_active_worktree_dirs(config)
    if worktree_dirs is not None:
        print(f"Worktree filter: {len(worktree_dirs)} active worktrees found", file=sys.stderr)

    session_files = []
    for project_dir in os.listdir(CLAUDE_PROJECTS_DIR):
        project_path = os.path.join(CLAUDE_PROJECTS_DIR, project_dir)
        if not os.path.isdir(project_path):
            continue

        # Apply worktree filter first (cheapest check)
        if worktree_dirs is not None and project_dir not in worktree_dirs:
            continue

        # Apply include/exclude filters on the project directory name
        included = any(p.search(project_dir) for p in include_patterns)
        excluded = any(p.search(project_dir) for p in exclude_patterns)
        if not included or excluded:
            continue

        for jsonl_file in glob(os.path.join(project_path, "*.jsonl")):
            session_files.append(jsonl_file)

    return session_files


def extract_session(
    jsonl_path: str,
    config: dict,
    state: dict,
    lookback_cutoff: datetime,
) -> dict | None:
    """Extract a compact summary from a single session JSONL file.

    Returns None if the session should be skipped (unchanged, too old, etc.).
    """
    file_size = os.path.getsize(jsonl_path)
    session_id = Path(jsonl_path).stem  # UUID from filename

    # Check if session has changed since last sync
    session_state = state.get("sessions", {}).get(session_id, {})
    last_file_size = session_state.get("last_file_size", 0)

    if file_size == last_file_size:
        return None  # No changes since last sync

    is_update = last_file_size > 0
    read_offset = last_file_size if is_update else 0

    max_msg_len = config["sessions"]["max_message_length"]
    records = []

    try:
        with open(jsonl_path, "rb") as f:
            if is_update:
                # For re-syncs, only read new lines
                f.seek(read_offset)

            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # Skip malformed lines
    except (OSError, IOError) as e:
        print(f"Warning: Could not read {jsonl_path}: {e}", file=sys.stderr)
        return None

    if not records:
        return None

    # Extract metadata — scan all records since slug/branch may appear later
    session_meta = {
        "session_id": session_id,
        "slug": "",
        "git_branch": "",
        "cwd": "",
        "version": "",
    }
    for r in records:
        if r.get("sessionId") and not session_meta.get("session_id"):
            session_meta["session_id"] = r["sessionId"]
        if r.get("slug") and not session_meta["slug"]:
            session_meta["slug"] = r["slug"]
        if r.get("gitBranch") and not session_meta["git_branch"]:
            session_meta["git_branch"] = r["gitBranch"]
        if r.get("cwd") and not session_meta["cwd"]:
            session_meta["cwd"] = r["cwd"]
        if r.get("version") and not session_meta["version"]:
            session_meta["version"] = r["version"]
        # Stop scanning once we have all metadata
        if all(session_meta.values()):
            break

    # Extract timestamps
    timestamps = [r.get("timestamp") for r in records if r.get("timestamp")]
    if not timestamps:
        return None

    first_ts = min(timestamps)
    last_ts = max(timestamps)

    # Check lookback window (use last timestamp)
    try:
        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        if last_dt < lookback_cutoff:
            return None
    except (ValueError, AttributeError):
        pass

    # If this is the full read (not update), also check first timestamp from the full file
    if not is_update:
        # For new sessions, use full-file timestamps
        all_records_with_ts = records
    else:
        # For updates, we need timestamps from new data but also know about the full file
        all_records_with_ts = records

    # Extract user messages
    user_messages = []
    for r in records:
        if r.get("type") != "user":
            continue
        msg = r.get("message", {})
        if isinstance(msg, dict):
            content = msg.get("content", "")
        elif isinstance(msg, str):
            content = msg
        else:
            continue
        # Handle content as list of content blocks (e.g., [{"type": "text", "text": "..."}])
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            content = "\n".join(text_parts)
        if isinstance(content, str) and content.strip():
            truncated = content[:max_msg_len]
            if len(content) > max_msg_len:
                truncated += "..."
            user_messages.append(truncated)

    # Extract token usage from assistant messages
    total_input_tokens = 0
    total_output_tokens = 0
    models_used = set()

    for r in records:
        if r.get("type") != "assistant":
            continue
        msg = r.get("message", {})
        if not isinstance(msg, dict):
            continue
        model = msg.get("model")
        if model:
            models_used.add(model)
        usage = msg.get("usage", {})
        if usage:
            total_input_tokens += (
                usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
            )
            total_output_tokens += usage.get("output_tokens", 0)

    # Calculate duration from turn_duration records
    total_duration_ms = 0
    for r in records:
        if r.get("type") == "system" and r.get("subtype") == "turn_duration":
            total_duration_ms += r.get("durationMs", 0)

    # Get linked issues from state
    existing_linear_issue_ids = session_state.get("linked_issues", [])

    # Get project directory name (for context)
    project_dir = Path(jsonl_path).parent.name

    # Look up PR for this branch via gh CLI
    pr_url = None
    pr_number = None
    pr_title = None
    git_branch = session_meta.get("git_branch", "")
    cwd = session_meta.get("cwd", "")
    if git_branch and cwd and os.path.isdir(cwd):
        try:
            result = subprocess.run(
                ["gh", "pr", "list", "--head", git_branch, "--json", "number,url,title", "--limit", "1"],
                capture_output=True, text=True, cwd=cwd, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                prs = json.loads(result.stdout)
                if prs:
                    pr_url = prs[0]["url"]
                    pr_number = prs[0]["number"]
                    pr_title = prs[0]["title"]
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, OSError):
            pass  # gh not available or not in a git repo — skip silently

    return {
        **session_meta,
        "project_dir": project_dir,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "user_messages": user_messages,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_duration_ms": total_duration_ms,
        "models_used": sorted(models_used),
        "is_update": is_update,
        "existing_linear_issue_ids": existing_linear_issue_ids,
        "file_size": file_size,
        "user_name": config.get("linear", {}).get("user_email", ""),
        "pr_url": pr_url,
        "pr_number": pr_number,
        "pr_title": pr_title,
    }


def main():
    parser = argparse.ArgumentParser(description="Extract Claude Code session summaries")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to config.yaml")
    parser.add_argument("--state", default=DEFAULT_STATE_PATH, help="Path to state.json")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be extracted without outputting full data")
    parser.add_argument("--session", help="Extract a specific session by UUID")
    args = parser.parse_args()

    config = load_config(args.config)
    state = load_state(args.state)

    # Calculate lookback cutoff
    from datetime import timedelta
    lookback_hours = config["sync"]["lookback_hours"]
    lookback_cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    # Find session files
    session_files = find_session_files(config)

    # If specific session requested, filter to just that one
    if args.session:
        session_files = [f for f in session_files if args.session in f]

    max_sessions = config["sync"]["max_sessions_per_run"]
    sessions = []

    for jsonl_path in sorted(session_files):
        if len(sessions) >= max_sessions:
            break

        summary = extract_session(jsonl_path, config, state, lookback_cutoff)
        if summary:
            sessions.append(summary)

    if args.dry_run:
        print(f"Found {len(session_files)} total session files", file=sys.stderr)
        print(f"Extracted {len(sessions)} sessions to sync", file=sys.stderr)
        for s in sessions:
            action = "UPDATE" if s["is_update"] else "NEW"
            print(
                f"  [{action}] {s['session_id'][:8]}... "
                f"slug={s['slug'] or 'N/A'} "
                f"msgs={len(s['user_messages'])} "
                f"tokens={s['total_input_tokens'] + s['total_output_tokens']}",
                file=sys.stderr,
            )
        return

    output = {
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "linear_team": config["linear"].get("default_team", config["linear"].get("default_project", "")),
            "user_email": config["linear"]["user_email"],
        },
        "sessions": sessions,
    }
    json.dump(output, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
