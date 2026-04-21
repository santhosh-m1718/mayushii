# Orchestrator Command Reference

Quick-reference for `mayushii` and `bd` commands. The [SKILL.md](SKILL.md) tells you how to think; this tells you what to type.

## Task Management (beads)

```bash
bd create "title" -t task -p 1                    # create a task
bd create "title" -t task -p 1 --deps orch-XX     # task with dependency
bd create "title" -t task --parent orch-XX         # child task
bd ready --json                                     # see unblocked tasks
bd show orch-XX --json                              # task details
bd children orch-XX --json                          # child tasks
bd close orch-XX --reason "summary"                 # complete a task
bd update orch-XX --status blocked --append-notes "why"
bd search "keyword"                                 # find tasks
```

## Worker Management

Workers auto-resolve repos from `repos/`. Use `--repo-name <name>` if multiple repos exist.

```bash
# Start workers
mayushii worker start orch-XX --role explore --skills debug,backend
mayushii worker start orch-XX --role edit --skills git,backend
mayushii worker start orch-XX --role verify --skills code-review
mayushii worker start orch-XX --role explore --auto-skills
mayushii worker start orch-XX --role edit --repo-name frontend

# Message workers
mayushii worker send orch-XX "message" --type nudge     # light context injection
mayushii worker send orch-XX "message" --type status    # /btw query (no context bloat)
mayushii worker send orch-XX "message" --type normal    # full conversation message
mayushii worker send orch-XX "message" --type divert    # Ctrl-C + redirect

# Manage workers
mayushii worker list                                     # show all workers
mayushii worker stop orch-XX                             # stop a worker
mayushii worker output orch-XX                           # capture recent output
mayushii worker output orch-XX --lines 50                # more output lines
```

## Monitoring

```bash
mayushii status                    # full dashboard with idle times
mayushii stalls                    # find workers idle > 10 min
mayushii stalls --threshold 5      # custom threshold in minutes
```

## Skill Selection

```bash
mayushii skill list                                    # see available skills
mayushii skill select "task description" --role explore # LLM picks skills
```

## Worker Signals

Workers signal the orchestrator automatically via tmux messages:

```
[Worker orch-XX]: done — <reason>        # task completed successfully
[Worker orch-XX]: failed — <reason>      # session ended without closing task
[Worker orch-XX]: stalled — no activity  # idle too long
[Worker orch-XX asks]: <question>        # worker needs guidance
```

## Worker Start Flags

| Flag | Purpose |
|------|---------|
| `--role` `-r` | Agent role: explore, plan, edit, verify |
| `--skills` `-s` | Comma-separated skill names to inject |
| `--context` `-c` | Context string from prior tasks |
| `--prompt` `-p` | Custom initial prompt (overrides default) |
| `--repo-name` | Repo name in repos/ directory |
| `--repo` | Explicit repo path (rarely needed) |
| `--auto-skills` | Let LLM pick skills based on task |

## Common Patterns

### Explore then edit (sequential)
```bash
bd create "Explore: investigate the bug" -t task -p 1
  # → orch-abc
bd create "Edit: implement the fix" -t task -p 1 --deps orch-abc
  # → orch-def
mayushii worker start orch-abc --role explore --skills debug,backend
# ... wait for orch-abc to complete ...
mayushii worker start orch-def --role edit --skills git,backend \
  --context "Explorer found the bug in auth.py:142 — session token not refreshed"
```

### Parallel exploration
```bash
bd create "Explore: check frontend for the issue" -t task -p 1
bd create "Explore: check backend for the issue" -t task -p 1
mayushii worker start orch-abc --role explore --repo-name frontend
mayushii worker start orch-def --role explore --repo-name backend
```

### Edit with follow-up review
```bash
bd create "Edit: implement feature" -t task -p 1
bd create "Verify: review the implementation" -t task -p 1 --deps orch-abc
```
