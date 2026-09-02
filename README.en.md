# Multi-Agent Hub

[中文版 README](README.md)

A pluggable multi-agent dispatch hub built on **MCP (Model Context Protocol)**.

The Master Agent is responsible for **breaking down tasks, dispatching work in parallel, and validating results**. Actual coding is delegated to multiple CLI Workers, each operating in its **own git worktree**, and only merged back to the main branch after passing tests.

The controller can be Claude Code, Claude Desktop, or Codex / Cursor. Workers support Claude Code, Antigravity (`agy`), Codex, and Ollama — whichever is installed on the machine gets used. Detection happens automatically at startup, and anything not installed is disabled automatically.

## Why worktree

The biggest pitfall in parallel dispatch is multiple agents editing the same file at once. Here, each subtask gets its own **independent worktree + independent branch**, providing physical isolation, and is finally converged with `git merge --no-ff`. If a conflict occurs, it runs `merge --abort` and reports which two subtasks collided, instead of letting the agent resolve it on its own.

## Architecture

```
Master Agent (Claude Code / Desktop / Cursor)
        │
        │ MCP over stdio
        ▼
mcp_worker_hub.py  ←── HUB_WORKERS / HUB_BIN_* / HUB_WAIT_SLICE
        │
   ┌────────────────┼────────────────┐
   ▼                 ▼                ▼
Git Worktree      CLI Workers     Docker Sandbox
(file isolation)  claude_cli      (runs tests,
+ .hub_prompt     agy_cli         no network by
                  codex_cli       default)
                  ollama
```

## Quick Start

Two installation paths — pick one.

### (a) As a Claude Code plugin (recommended, no clone needed)

This repo ships with `.claude-plugin/marketplace.json`, making it a single-plugin marketplace on its own:

```
claude plugin marketplace add zkylek1212-k/multi-agent-hub
claude plugin install multi-agent-hub@multi-agent-hub
```

(Inside a Claude Code conversation, use `/plugin marketplace add ...` and `/plugin install ...`.)

**Do not skip step three**: the plugin only ships files and MCP configuration — it does **not** install the Python dependency (`mcp[cli]`). Without it, the MCP server won't start and `agent-hub` won't show up under `/mcp`. After installing, run:

```powershell
powershell -ExecutionPolicy Bypass -File (Get-ChildItem "$env:USERPROFILE\.claude\plugins\cache\multi-agent-hub\multi-agent-hub\*\install.ps1" | Sort-Object FullName -Descending | Select-Object -First 1).FullName -DepsOnly
```

`-DepsOnly` only "installs dependencies + detects Workers + runs self-test" — it will not `git init` inside the plugin directory, nor register a duplicate local-scope agent-hub (which would cause double-loading). Restart Claude Code once it finishes.

Success check: `/mcp` shows `agent-hub` as connected, and `multi-agent-dispatch` appears in the skill list.

> ⚠️ **The plugin path currently only supports Windows** — `.mcp.json` launches via `py -3` (the Windows Python launcher). For macOS / Linux, use path (b) below and manually write `.mcp.json` following [INSTALL.md §3](INSTALL.md).

### (b) Clone and run the script directly

On Windows, after cloning, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The script detects tools, installs dependencies, generates `.mcp.json` (preserving it if a plugin-installed version is detected), and finally runs a self-test. If `docker` is not detected, it will **attempt to auto-install Docker Desktop via winget** (a UAC prompt will appear); Python, git, and Worker CLIs are only detected, not installed.

For prerequisites, macOS / Linux instructions, and troubleshooting, see **[INSTALL.md](INSTALL.md)**.

## MCP Server and Skill

This project provides two things together, and both are required:

| Provides | Files |
|---|---|
| **MCP server** (`agent-hub`) | **Capabilities**: 7 tools for dispatch, worktree management, sandbox testing, etc. | `mcp_worker_hub.py` + `.mcp.json` |
| **Skill** (`multi-agent-dispatch`) | **Instructions**: dispatch SOP — how to break down tasks, dispatch in parallel, and validate convergence | `skills/multi-agent-dispatch/SKILL.md` |

**MCP provides capability, Skill provides instructions.** With only MCP, the Master has the tools but not the correct workflow (it easily ends up finishing one task before dispatching the next, or claiming completion without testing). With only the Skill, it's just a document that can't actually be executed.

The Skill is installed together with the plugin and triggers automatically whenever the user mentions parallel dispatch or splitting work across multiple workers. If using path (b), `install.ps1` will still copy `MASTER_SOP.md` to `CLAUDE.md`.

## Tools

| Tool | Purpose |
|---|---|
| `get_active_workers` | Reports which Workers are enabled this session and the executables actually resolved |
| `git` | worktree add / remove, diff, log, merge |
| `delegate_to_worker` | Asynchronous dispatch, returns a job_id immediately |
| `wait_for_job` | Waits for a whole batch of jobs at once (wait duration is configurable, to avoid client timeout limits) |
| `check_job_status` | Non-blocking query of a single job |
| `list_jobs` | Status table of all jobs: job_id / Worker / status / duration / task |
| `run_in_sandbox` | Runs tests against a worktree inside a container (network disabled by default) |

## ⚠️ Security Model

Isolation is **layered and incomplete** — please understand this before use:

| Stage | Isolation level |
|---|---|
| Worker writes code | **No permission isolation.** The Worker runs directly on your machine with `--dangerously-skip-permissions`. Worktree isolation covers "file versions," not "what it's allowed to do" |
| Running tests | Isolated. Docker container + no network by default + memory/CPU limits |
| Master reads Worker output | **No isolation.** The Worker's stdout flows directly into the Master's context |

**The Docker sandbox protects "testing," not "coding." Only use this on projects you trust.**

## Documentation

| File | Content |
|---|---|
| [INSTALL.md](INSTALL.md) | Installation and deployment, tool list, FAQ |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Architecture rationale, design decisions, validation records |
| [MASTER_SOP.md](MASTER_SOP.md) | Master's system prompt (`install.ps1` copies this to `CLAUDE.md`) |
| [skills/multi-agent-dispatch/SKILL.md](skills/multi-agent-dispatch/SKILL.md) | Skill version of the dispatch SOP, installed with the plugin and triggered on demand |

## License

[MIT](LICENSE)
