# shared-skills

Shared Claude Code skills for the Klovr team.

## Skills

### sync-linear
Syncs Claude Code session data to Linear issues. Matches sessions to existing issues or creates new ones with summaries and todos. Designed for team collaboration — each member runs their own sync against a shared Linear project.

See [sync-linear/SKILL.md](sync-linear/SKILL.md) for full setup and usage.

## Installing a skill

```bash
npx skills add klovr-co/shared-skills
```

Or manually:

```bash
cp -r sync-linear ~/.claude/skills/sync-linear
```
