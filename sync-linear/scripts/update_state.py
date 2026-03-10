#!/usr/bin/env python3
"""Update sync state with results from a Claude-to-Linear sync run.

Reads sync results JSON from stdin and merges into state.json.
Maintains many-to-many mappings between sessions and Linear issues.
"""

import json
import os
import sys
from datetime import datetime, timezone

DEFAULT_STATE_PATH = os.path.expanduser("~/.claude-linear-sync/state.json")


def load_state(state_path: str) -> dict:
    """Load existing state or create empty state."""
    if not os.path.exists(state_path):
        return {"last_run": None, "sessions": {}, "issues": {}}
    try:
        with open(state_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception):
        return {"last_run": None, "sessions": {}, "issues": {}}


def merge_state(state: dict, sync_results: dict) -> dict:
    """Merge sync results into existing state.

    Expected sync_results format:
    {
        "synced": [
            {
                "session_id": "uuid-1",
                "file_size": 45678,
                "action": "created" | "updated",
                "linear_issue_ids": ["KLO-123", "KLO-456"]
            }
        ]
    }
    """
    now = datetime.now(timezone.utc).isoformat()
    state["last_run"] = now

    for result in sync_results.get("synced", []):
        session_id = result["session_id"]
        file_size = result.get("file_size", 0)
        issue_ids = result.get("linear_issue_ids", [])

        # Update session state
        if session_id not in state["sessions"]:
            state["sessions"][session_id] = {
                "last_file_size": file_size,
                "linked_issues": [],
            }

        session_state = state["sessions"][session_id]
        session_state["last_file_size"] = file_size

        # Add new issue links (deduplicated)
        existing_links = set(session_state["linked_issues"])
        for issue_id in issue_ids:
            existing_links.add(issue_id)
        session_state["linked_issues"] = sorted(existing_links)

        # Update issue state (reverse mapping)
        for issue_id in issue_ids:
            if issue_id not in state["issues"]:
                state["issues"][issue_id] = {
                    "linked_sessions": [],
                }
            issue_state = state["issues"][issue_id]
            existing_sessions = set(issue_state["linked_sessions"])
            existing_sessions.add(session_id)
            issue_state["linked_sessions"] = sorted(existing_sessions)

    return state


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Update Claude-Linear sync state")
    parser.add_argument("--state", default=DEFAULT_STATE_PATH, help="Path to state.json")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated without writing")
    args = parser.parse_args()

    # Read sync results from stdin
    try:
        sync_results = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON from stdin: {e}", file=sys.stderr)
        sys.exit(1)

    state = load_state(args.state)
    updated_state = merge_state(state, sync_results)

    if args.dry_run:
        print(f"Would update state with {len(sync_results.get('synced', []))} sync results", file=sys.stderr)
        print(f"Total sessions tracked: {len(updated_state['sessions'])}", file=sys.stderr)
        print(f"Total issues tracked: {len(updated_state['issues'])}", file=sys.stderr)
        return

    # Ensure state directory exists
    state_dir = os.path.dirname(args.state)
    if state_dir:
        os.makedirs(state_dir, exist_ok=True)

    # Write atomically (write to tmp, then rename)
    tmp_path = args.state + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(updated_state, f, indent=2)
    os.replace(tmp_path, args.state)

    print(f"State updated: {len(sync_results.get('synced', []))} sessions synced", file=sys.stderr)


if __name__ == "__main__":
    main()
