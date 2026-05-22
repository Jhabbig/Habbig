# Claude Code Superpowers — Installed Bundle

This repo ships a curated set of Claude Code skills, agents, plugins, and MCP servers, committed under `.claude/` and `.mcp.json`. Everything below is auto-loaded when you open this project in Claude Code.

## What's installed

### Skills (`.claude/skills/`)
15 skills — 14 from [obra/superpowers](https://github.com/obra/superpowers) (brainstorming, TDD, systematic debugging, parallel-agent dispatch, plan writing, code review, git worktrees, verification-before-completion, writing-skills) plus `confidence-check` from SuperClaude.

### Agents (`.claude/agents/`)
375 unique subagents combined from:
- [wshobson/agents](https://github.com/wshobson/agents) — 126 agents
- [VoltAgent/awesome-claude-code-subagents](https://github.com/VoltAgent/awesome-claude-code-subagents) — gap-fill from 162 agents
- [0xfurai/claude-code-subagents](https://github.com/0xfurai/claude-code-subagents) — gap-fill from 139 agents
- [SuperClaude-Org/SuperClaude_Framework](https://github.com/SuperClaude-Org/SuperClaude_Framework) — 17 agents
Dedup rule: first-wins on filename. Full wshobson plugin marketplace preserved under `.claude/plugins/wshobson/`.

### Commands (`.claude/commands/`)
30 SuperClaude slash commands: `/sc:analyze`, `/sc:brainstorm`, `/sc:build`, `/sc:implement`, `/sc:design`, `/sc:document`, `/sc:estimate`, `/sc:explain`, `/sc:improve`, `/sc:git`, `/sc:pm`, `/sc:research`, `/sc:test`, `/sc:troubleshoot`, `/sc:workflow`, etc.

### MCP servers (`.mcp.json`)
- **playwright** — `@playwright/mcp` for browser automation.
- **repomix** — pack codebases for AI analysis.
- **obsidian** — vault integration (needs `OBSIDIAN_API_KEY`).
- **context7** — live, version-specific library docs (`@upstash/context7-mcp`).
- **sequential-thinking** — structured multi-step reasoning.
- **memory** — persistent knowledge graph across sessions (stores in `.claude/memory.json`).
- **sentry** — error tracking integration (needs `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`).
- **chrome-devtools** — live browser inspection (network, console, perf).
- **fetch** — HTTP requests for arbitrary URLs.
- **time** — timezone/date utilities.

### Hooks (`.claude/settings.json`)
- `SessionStart` runs the superpowers session-start hook.
- `PostToolUse` after Edit/Write defers to `npx tdd-guard` if installed (no-op otherwise — install with `npm i -D tdd-guard` per-project to activate).

## CLI-only tools (not vendored — install separately)

These don't live inside Claude Code itself; they're external CLIs you run alongside it.

| Tool | Install | What it does |
|------|---------|--------------|
| [Claude Squad](https://github.com/smtg-ai/claude-squad) | `brew install claude-squad` or `go install github.com/smtg-ai/claude-squad@latest` | TUI for orchestrating multiple Claude Code sessions in tmux. |
| [Repomix CLI](https://github.com/yamadashy/repomix) | `npx repomix` | Pack a repo into one file for sharing/upload. The MCP version is already wired above. |
| [Obsidian](https://obsidian.md) + [obsidian-mcp](https://github.com/StevenStavrakis/obsidian-mcp) | npm/manual | Vault-as-knowledge-base for Claude. Add to `.mcp.json` once your vault path is set. |

## Skipped / unavailable

- **obra/subagents-stop-hook** — repo wasn't publicly accessible at install time. If/when it goes public, add it under `.claude/hooks/`.
- **Awesome Claude Code list** ([hesreallyhim/awesome-claude-code](https://github.com/hesreallyhim/awesome-claude-code)) — it's a curated link list, not installable. Browse there for more skills/agents to add to this bundle.

## Updating

Re-run the install by re-cloning the upstreams into a temp dir and copying:

```bash
git clone --depth=1 https://github.com/obra/superpowers /tmp/sp && cp -r /tmp/sp/skills/* .claude/skills/
git clone --depth=1 https://github.com/wshobson/agents /tmp/wa && rm -rf .claude/plugins/wshobson && cp -r /tmp/wa/plugins .claude/plugins/wshobson
```
