#!/usr/bin/env python3
"""mcp_server.py - MCP server exposing the claude-obsidian engine for AgentLoop.

Dumb service: no LLM inside. Each tool maps 1:1 to a primitive in
scripts/engine.py. The server mediates vault writes (lock -> write -> release ->
commit) so agents WITHOUT direct filesystem access (Pi, Hermes) can mutate the
vault safely and concurrently with other writers. Claude gets the same tools as
native MCP tool-calls.

Run via uv (fetches the `mcp` SDK on demand; no venv to manage):
    uv run --with mcp python scripts/mcp_server.py

Register with Claude Code (WIKI_VAULT pins the served vault):
    claude mcp add agentloop -e WIKI_VAULT=/path/to/AgentLoop -- \
        uv run --with mcp python /abs/path/to/scripts/mcp_server.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make sibling engine.py importable regardless of the agent's cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import engine  # noqa: E402  (the primitives this server exposes)

VAULT = os.environ.get("WIKI_VAULT", "").strip()
if not VAULT:
    print("ERR: WIKI_VAULT env var must be set for the agentloop MCP server.",
          file=sys.stderr)
    sys.exit(2)
os.environ["WIKI_VAULT"] = VAULT  # engine.vault_root() reads this

mcp = FastMCP("agentloop")


@mcp.tool()
def wiki_status() -> dict:
    """Vault + engine status: root, mode, transport, locks held, git present."""
    return engine.status()


@mcp.tool()
def wiki_route(content_type: str, name: str) -> str:
    """Return the vault-relative filing path for new content.
    content_type is one of: source, entity, concept, session, research.
    The agent writes generated page bodies to this path via wiki_write."""
    return engine.route(content_type, name)


@mcp.tool()
def wiki_lock_list() -> dict:
    """List currently-held per-file locks: path, holder pid/host/agent, stale flag."""
    return engine.lock_list()


@mcp.tool()
def wiki_lock_acquire(path: str, timeout: int = 60) -> dict:
    """Acquire an advisory lock on a vault-relative path. Blocks up to `timeout`
    seconds if another writer holds it. Used for multi-page critical sections."""
    return engine.lock_acquire(path, timeout)


@mcp.tool()
def wiki_lock_release(path: str) -> dict:
    """Release an advisory lock. Cooperative and cross-process: the agent that
    acquired it (possibly a different process) is the one that releases it."""
    return engine.lock_release(path)


@mcp.tool()
def wiki_write(path: str, content: str) -> dict:
    """Atomic, concurrency-safe write of a full page. Acquires the file lock,
    writes (atomic tmp+rename), releases the lock, then commits to git. Use this
    for normal page writes. Returns the write result plus the commit result."""
    acquired = engine.lock_acquire(path, timeout=60)
    if not acquired.get("acquired"):
        return {"written": False, "reason": "lock acquire failed", "acquire": acquired}
    try:
        result = engine.write(path, content, auto_commit=False)
    finally:
        engine.lock_release(path)
    result["commit"] = engine.commit()
    return result


@mcp.tool()
def wiki_read(path: str) -> dict:
    """Read a vault-relative file. Returns {exists, path, content}."""
    return engine.read(path)


@mcp.tool()
def wiki_log(detail: str, op: str = "-", agent: str = "") -> dict:
    """Append a row to wiki/log.md (newest first). Set `agent` to your name
    (pi/hermes/claude) so the log records who did what."""
    return engine.log_append(detail, op=op, agent=agent or None)


@mcp.tool()
def wiki_hot_get() -> dict:
    """Read the hot cache (wiki/hot.md) - recent-context summary."""
    return engine.hot_get()


@mcp.tool()
def wiki_hot_set(content: str) -> dict:
    """Overwrite the hot cache (wiki/hot.md). Overwrite, not append."""
    return engine.hot_set(content)


@mcp.tool()
def wiki_index() -> dict:
    """Read the master index (wiki/index.md)."""
    return engine.index_get()


@mcp.tool()
def wiki_lint() -> dict:
    """Mechanical health check: dead wikilinks, orphan pages, frontmatter gaps.
    Read-only. Semantic fixes (merging, rewriting) stay with the calling agent."""
    return engine.lint()


@mcp.tool()
def wiki_commit(message: str = "") -> dict:
    """Stage wiki/ .raw/ .vault-meta/ and commit if anything changed. Skips while
    locks are held. Usually you don't need this - wiki_write commits for you."""
    return engine.commit(message or None)


if __name__ == "__main__":
    mcp.run()
