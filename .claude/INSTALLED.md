# Claude Code Superpowers — Installed Bundle

This repo ships a curated set of Claude Code skills, agents, plugins, and MCP servers, committed under `.claude/` and `.mcp.json`. Everything below is auto-loaded when you open this project in Claude Code.

## What's installed

### Skills (`.claude/skills/`) — from [obra/superpowers](https://github.com/obra/superpowers)
14 skills covering brainstorming, TDD, systematic debugging, parallel-agent dispatch, plan writing, code review, git worktrees, verification-before-completion, and the meta-skill for writing new skills.

### Agents (`.claude/agents/`) — from [wshobson/agents](https://github.com/wshobson/agents)
126 unique subagent definitions, flattened from the source plugin marketplace (first occurrence wins on name collisions). Invoke via the `Agent` tool with `subagent_type=<name>`. The full 81-plugin marketplace is preserved under `.claude/plugins/wshobson/` if you want the plugin-style grouping.

### MCP servers (`.mcp.json`)
- **playwright** — `@playwright/mcp` for browser automation (snapshots, click, type, network inspection).
- **repomix** — pack this codebase or any remote repo into a single AI-readable file. Useful for cross-file review.

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
