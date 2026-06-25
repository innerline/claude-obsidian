# Engine Guide

`scripts/engine.py` is the vault-agnostic write/maintain layer of the LLM-Wiki
pattern. It decouples vault access from the scripts directory so any vault (set
via `WIKI_VAULT`) can be served, and so multiple agents can share one vault
safely. Read/QA is handled by Miyo; the engine does not retrieve.

## Vault selection

All primitives operate on the vault named by the `WIKI_VAULT` environment
variable (`vault_root()` resolves and validates it). The CLI and MCP server
forward this through. Unset → the engine errors immediately.

## Primitives

| Function | Purpose |
|---|---|
| `route(ctype, name)` | Vault-relative filing path for new content; delegates to `wiki-mode.py`. `ctype`: source\|entity\|concept\|session\|research. |
| `lock_acquire(rel, timeout=60)` | Age-based per-file advisory lock; blocks up to `timeout`s if held. |
| `lock_release(rel)` | Cooperative, cross-process release (the acquirer and releaser may be different processes). |
| `lock_list()` | Currently-held locks with holder pid/host/agent + stale flag. |
| `write(rel, content, auto_commit=True)` | Atomic write (tmp + `os.replace`); commits by default. |
| `read(rel)` | Read a vault-relative file. |
| `log_append(detail, op, agent)` | Prepend a row to `wiki/log.md` (newest first). |
| `hot_get()` / `hot_set(content)` | Read / overwrite `wiki/hot.md`. |
| `index_get()` | Read `wiki/index.md`. |
| `lint()` | Mechanical health check: dead wikilinks, orphans, frontmatter gaps. Read-only. |
| `commit(message=None)` | Stage `wiki/ .raw/ .vault-meta/` and commit if changed; skips while locks are held. Agent-neutral replacement for the Claude-only PostToolUse hook. |
| `status()` | Vault root, engine path, mode, transport, locks held, git present. |

Path safety: every primitive that takes a path funnels through `_safe_rel`,
which rejects empty, absolute, traversal (`../`), control-char, and symlink-
escape paths (exit 2).

## Concurrency model

Locks are **age-based records on disk** (`<vault>/.vault-meta/locks/<hash>.lock`),
not BSD `flock` — so a lock survives across processes, which matters because
`lock acquire`, `write`, and `lock release` are normally three separate process
invocations by different agents. A meta-file `fcntl` serializes record I/O.
Stale-after defaults to 60s (`WIKI_LOCK_STALE`), so a crashed agent's lock
self-clears. Set `WIKI_AGENT` so the log records who did what.

## Interfaces

**CLI** — `bin/wiki <command>` (targets `WIKI_VAULT`, or `--vault`, or the path
in the machine-local `.served-vault`):
```bash
wiki status
wiki route concept "Sparse Retrieval"
wiki lock acquire wiki/resources/concepts/Foo.md
printf -- '---\n...' | wiki write wiki/resources/concepts/Foo.md --stdin
wiki lock release wiki/resources/concepts/Foo.md
wiki lint --json
wiki commit
```

**MCP server** — `scripts/mcp_server.py` (FastMCP), run via uv:
```bash
uv run --with mcp python scripts/mcp_server.py
```
Register (Claude Code):
```bash
claude mcp add agentloop -e WIKI_VAULT=/path/to/vault -- \
    uv run --with mcp python /abs/path/to/claude-obsidian/scripts/mcp_server.py
```
Exposes 13 tools — `wiki_status`, `wiki_route`, `wiki_lock_acquire/release/list`,
`wiki_write`, `wiki_read`, `wiki_log`, `wiki_hot_get/set`, `wiki_index`,
`wiki_lint`, `wiki_commit`. `wiki_write` performs the full
lock → write → release → commit cycle so agents without filesystem access can
mutate the vault.

## Tests

```bash
make test-engine   # path-safety, locking, commit (hermetic, system python3)
make test-mcp      # tool registration + delegation (requires uv + mcp SDK)
```
