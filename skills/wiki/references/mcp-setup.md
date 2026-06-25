# MCP Setup

MCP lets Claude read and write vault notes directly without copy-paste. Five options ordered from simplest to most featureful.

> [!tip] Recommendation
> If you have **Obsidian v1.12 or newer**, start with **Option D: Obsidian CLI**. It needs no MCP server, no plugins, and no TLS workarounds. Use Options A or B only if you need persistent MCP integration or are on an older Obsidian version.

---

## Step 1: Install the Local REST API Plugin

You must do this in Obsidian (Claude cannot do it programmatically):

1. Obsidian > Settings > Community Plugins > Turn off Restricted Mode
2. Browse > Search "Local REST API" > Install > Enable
3. Settings > Local REST API > Copy the API key

The plugin runs on `https://127.0.0.1:27124` with a self-signed certificate.

Test it:
```bash
curl -sk -H "Authorization: Bearer <YOUR_KEY>" https://127.0.0.1:27124/
```

You should get a JSON response with vault info.

---

## Option A: mcp-obsidian (REST API based)

Uses MarkusPfundstein's mcp-obsidian. Requires the Local REST API plugin running.

```bash
claude mcp add-json obsidian-vault '{
  "type": "stdio",
  "command": "uvx",
  "args": ["mcp-obsidian"],
  "env": {
    "OBSIDIAN_API_KEY": "<YOUR_KEY>",
    "OBSIDIAN_HOST": "127.0.0.1",
    "OBSIDIAN_PORT": "27124",
    "NODE_TLS_REJECT_UNAUTHORIZED": "0"
  }
}' --scope user
```

> [!warning] Security
> `NODE_TLS_REJECT_UNAUTHORIZED: "0"` **disables TLS certificate verification process-wide** for the MCP server. It is required here because the Local REST API plugin uses a self-signed certificate. This is acceptable for `127.0.0.1` (localhost) connections only. Never use this setting for any non-loopback connection. If you are uncomfortable with the global TLS bypass, prefer **Option D (Obsidian CLI)** or **Option B (filesystem-based)** which avoid this entirely.

Capabilities: read notes, write notes, search, patch frontmatter fields, append under headings.

---

## Option B: MCPVault (filesystem based)

No Obsidian plugin needed. Reads the vault directory directly.

```bash
claude mcp add-json obsidian-vault '{
  "type": "stdio",
  "command": "npx",
  "args": ["-y", "@bitbonsai/mcpvault@latest", "/absolute/path/to/your/vault"]
}' --scope user
```

Replace `/absolute/path/to/your/vault` with the actual vault path.

Tools available: `search_notes` (BM25), `read_note`, `create_note`, `update_note`, `get_frontmatter`, `update_frontmatter`, `list_all_tags`, `read_multiple_notes`.

---

## Option C: Direct REST API via curl

No MCP needed. Use curl in bash throughout the session. See `rest-api.md` for all commands.

---

## Option D: Obsidian CLI (recommended for v1.12+)

Obsidian shipped a native CLI in v1.12 (2026). It exposes vault operations directly to the terminal. No REST API plugin, no MCP server, no self-signed certs, no TLS workarounds. Claude calls it through the Bash tool.

**Check if available:**
```bash
which obsidian-cli 2>/dev/null && obsidian-cli --version
# or, on flatpak:
flatpak run md.obsidian.Obsidian --cli --version
```

**Common operations:**
```bash
# List all notes in a folder
obsidian-cli list /path/to/vault wiki/

# Read a note
obsidian-cli read /path/to/vault wiki/index.md

# Create or update a note
obsidian-cli write /path/to/vault wiki/new-note.md < content.md

# Search notes by content
obsidian-cli search /path/to/vault "query term"
```

**Why prefer this**:
- No plugin install required (CLI is built into Obsidian)
- No MCP server process to manage
- No TLS certificate bypass needed
- Survives Obsidian restarts (no persistent connection)
- Works identically across desktop and headless environments

**When to use Options A/B/C instead**: If you need persistent semantic search, frontmatter patching, or are on Obsidian < v1.12.

The `kepano/obsidian-skills` repo includes an `obsidian-cli` skill that wraps these commands as reusable patterns. Install it alongside this plugin for first-class CLI support.

---

## Option E: agentloop MCP server (vault-agnostic, cross-agent)

Uses the native FastMCP server at `scripts/mcp_server.py`. Exposes all engine primitives as `wiki_*` tools. No Obsidian plugin or external server needed—just the claude-obsidian repo.

```bash
claude mcp add agentloop -e WIKI_VAULT=/path/to/your/vault -- \
    uv run --with mcp python /absolute/path/to/claude-obsidian/scripts/mcp_server.py
```

Replace `/absolute/path/to/claude-obsidian` with the actual repo path and `/path/to/your/vault` with your vault path.

**Available tools (13):**
- `wiki_status()` - Vault + engine status
- `wiki_route(content_type, name)` - Filing path for new content
- `wiki_lock_acquire/release/list()` - Per-file advisory locks
- `wiki_write/read()` - Atomic I/O with locking
- `wiki_log(detail, op, agent)` - Append to wiki/log.md
- `wiki_hot_get/set()` - Hot cache (wiki/hot.md)
- `wiki_index()` - Read master index (wiki/index.md)
- `wiki_lint()` - Mechanical health check
- `wiki_commit(message)` - Git stage + commit

**Why prefer this**:
- No external dependencies beyond `mcp` (fetched by uv on demand)
- Vault-agnostic: serves any vault via WIKI_VAULT
- Cross-agent: Pi, Hermes, Claude can all access the same vault
- Agent-neutral: no Claude-specific hooks

**When to use Options A-D instead**: If you prefer REST API integration or want to use existing MCP server infrastructure.

---

## Use `--scope user`

Both MCP options use `--scope user` so the vault is available across all Claude Code projects, not just the one where you ran the command.

---

## Verification

After setup:

```bash
claude mcp list               # confirm the server appears
claude mcp get obsidian-vault # confirm the path or URL is correct
```

In a Claude Code session, type `/mcp` to check connection status.

Then test: "List all notes in my wiki folder."
