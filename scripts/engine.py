#!/usr/bin/env python3
"""engine.py - vault-agnostic primitives for the claude-obsidian engine.

The "write/maintain" half of the LLM-Wiki pattern, decoupled from any one
vault. Operates on the vault named by $WIKI_VAULT (set by the caller, the
`bin/wiki` CLI, or the MCP server). Read/QA is left to Miyo; this engine does
not retrieve.

Design notes:
- macOS-native. Locking is age-based with on-disk records (mirrors
  scripts/wiki-lock.sh semantics) so a lock survives across processes - the
  `lock acquire` / `write` / `lock release` trio are normally separate process
  invocations by different agents (Pi, Hermes, Claude), and BSD flock(2) would
  die with each process. Python `fcntl` is used only for the brief meta-file
  serialization, so no external `flock` binary is required.
- Python 3.9 compatible (the macOS system python3). No PEP-604 runtime unions.
- `commit()` replaces the Claude-Code-only PostToolUse hook with an
  agent-neutral equivalent: any writer that calls write(auto_commit=True) - or
  commit() directly - persists consistently, regardless of which agent drove it.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# ── paths ────────────────────────────────────────────────────────────────────
# SCRIPTS_DIR is always THIS file's dir (the repo's scripts/) - never the vault.
# VAULT_ROOT is the served vault ($WIKI_VAULT), independent of where we live.
SCRIPTS_DIR = Path(__file__).resolve().parent


def vault_root() -> Path:
    v = os.environ.get("WIKI_VAULT", "").strip()
    if not v:
        die("WIKI_VAULT is not set. Pass --vault <path> or export WIKI_VAULT.")
    p = Path(v).expanduser().resolve()
    if not p.is_dir():
        die(f"WIKI_VAULT is not a directory: {p}")
    return p


def die(msg: str, code: int = 2) -> None:
    print(f"ERR: {msg}", file=sys.stderr)
    sys.exit(code)


# ── sibling-script runner ────────────────────────────────────────────────────
def _interpreter(path: Path) -> str:
    return "bash" if path.suffix == ".sh" else sys.executable


def _script(name: str, *args: str) -> subprocess.CompletedProcess:
    """Run a sibling script with WIKI_VAULT pinned to our vault."""
    path = SCRIPTS_DIR / name
    env = dict(os.environ)
    env["WIKI_VAULT"] = str(vault_root())
    return subprocess.run(
        [_interpreter(path), str(path), *[str(a) for a in args]],
        capture_output=True, text=True, env=env,
    )


# ── route / mode ─────────────────────────────────────────────────────────────
def route(ctype: str, name: str) -> str:
    """Return the vault-relative filing path for new content of `ctype`."""
    cp = _script("wiki-mode.py", "route", ctype, name)
    if cp.returncode != 0:
        die(f"wiki-mode.py route failed: {cp.stderr.strip() or cp.stdout.strip()}", cp.returncode)
    return cp.stdout.strip()


def mode() -> Dict:
    cp = _script("wiki-mode.py", "config")
    return json.loads(cp.stdout) if cp.returncode == 0 else {"mode": "unknown"}


def status() -> Dict:
    vr = vault_root()
    out = {"vault_root": str(vr), "engine": str(SCRIPTS_DIR.parent)}
    out.update(mode())
    tp = vr / ".vault-meta" / "transport.json"
    if tp.exists():
        try:
            t = json.loads(tp.read_text())
            out["transport"] = t.get("preferred", "unknown")
        except json.JSONDecodeError as e:
            print(f"WARN: transport.json is corrupted: {e}", file=sys.stderr)
            out["transport"] = "unknown"
            out["transport_error"] = "json_decode_error"
        except OSError as e:
            print(f"WARN: cannot read transport.json: {e}", file=sys.stderr)
            out["transport"] = "unknown"
            out["transport_error"] = str(e)
    else:
        out["transport"] = "unknown"
    out["locks_held"] = len(lock_list().get("locks", []))
    out["git"] = (vr / ".git").exists()
    return out


# ── locking (age-based records; macOS-native meta-serialization) ─────────────
STALE_AFTER = int(os.environ.get("WIKI_LOCK_STALE", "60"))
HOST = os.uname().nodename


def _locks_dir() -> Path:
    d = vault_root() / ".vault-meta" / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def _meta_lock():
    """Context manager: exclusive fcntl on a meta file to serialize record I/O."""
    meta = vault_root() / ".vault-meta" / ".engine.meta.lock"
    meta.parent.mkdir(parents=True, exist_ok=True)  # .vault-meta may not exist yet
    f = open(meta, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield f
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def _lockfile(rel: str) -> Path:
    h = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    return _locks_dir() / f"{h}.lock"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_lock(rel: str) -> Optional[Dict]:
    lf = _lockfile(rel)
    if not lf.exists():
        return None
    try:
        return json.loads(lf.read_text())
    except json.JSONDecodeError as e:
        print(f"WARN: Lock file corrupted for {rel}: {e}", file=sys.stderr)
        return {"path": rel, "pid": -1, "host": "?", "acquired_at": "1970-01-01T00:00:00Z", "corrupt": True, "error": "json_decode"}
    except OSError as e:
        print(f"WARN: Cannot read lock file for {rel}: {e}", file=sys.stderr)
        return {"path": rel, "pid": -1, "host": "?", "acquired_at": "1970-01-01T00:00:00Z", "corrupt": True, "error": str(e)}


def _is_stale(rec: Dict) -> bool:
    acquired_at = rec.get("acquired_at", "")
    if not acquired_at or not isinstance(acquired_at, str):
        print(f"WARN: Lock has invalid acquired_at: {acquired_at!r}, treating as stale", file=sys.stderr)
        return True
    try:
        acquired = datetime.strptime(acquired_at, "%Y-%m-%dT%H:%M:%SZ")
        age = (datetime.now(timezone.utc) - acquired.replace(tzinfo=timezone.utc)).total_seconds()
    except ValueError as e:
        print(f"WARN: Lock has malformed timestamp {acquired_at!r}: {e}, treating as stale", file=sys.stderr)
        return True
    return age > STALE_AFTER


def lock_acquire(rel: str, timeout: int = STALE_AFTER) -> Dict:
    """Block up to `timeout`s for an exclusive lock on a vault-relative path."""
    rel = _safe_rel(rel)
    deadline = time.time() + timeout
    while True:
        with _meta_lock():
            rec = _read_lock(rel)
            if rec is None or _is_stale(rec):
                if rec is not None:
                    pass  # stale -> reclaim
                record = {"path": rel, "pid": os.getpid(), "host": HOST,
                          "acquired_at": _now_iso(), "agent": os.environ.get("WIKI_AGENT", "agent")}
                _lockfile(rel).write_text(json.dumps(record))
                return {"acquired": True, "path": rel, **record}
        if time.time() >= deadline:
            return {"acquired": False, "path": rel, "reason": "timeout",
                    "held_by": rec}
        time.sleep(0.2)


def lock_release(rel: str, force: bool = False) -> Dict:
    # Cooperative release: `acquire` and `release` are normally separate process
    # invocations (different PIDs), so we do NOT match on pid/host. Cross-process
    # release is allowed by design (mirrors scripts/wiki-lock.sh); staleness is
    # the only crash-safety net. `force` is accepted for API symmetry.
    rel = _safe_rel(rel)
    with _meta_lock():
        rec = _read_lock(rel)
        if rec is None:
            return {"released": True, "path": rel, "note": "not held"}
        _lockfile(rel).unlink(missing_ok=True)
        return {"released": True, "path": rel, "forced": force, "released_holder": rec}


def lock_list() -> Dict:
    with _meta_lock():
        held = []
        for lf in _locks_dir().glob("*.lock"):
            try:
                rec = json.loads(lf.read_text())
                rec["stale"] = _is_stale(rec)
                held.append(rec)
            except Exception:
                held.append({"file": lf.name, "corrupt": True})
    return {"locks": held, "count": len(held), "stale_after_sec": STALE_AFTER}


# ── path safety + file I/O ───────────────────────────────────────────────────
def _safe_rel(rel: str) -> str:
    """Normalize and validate a vault-relative path; reject escapes."""
    if not rel:
        die("path cannot be empty")
    if rel.startswith("/"):
        die("path must be vault-relative, not absolute")
    if '\n' in rel or '\r' in rel:
        die("path may not contain newlines or carriage returns")

    rel = rel.lstrip("/")
    vr = vault_root()

    # Check for symlink escape before full resolution
    # (matches wiki-lock.sh lines 124-140)
    try:
        resolved = vr.resolve()
        target = (vr / rel).resolve()
        if resolved != target:
            # os.path.commonpath returns a str; coerce to Path so the
            # comparison is type-consistent (str != Path is always True,
            # which would wrongly reject every nested path).
            common = Path(os.path.commonpath([resolved, target]))
            if common != resolved:
                die(f"path resolves outside vault via symlink: {rel}")
    except (ValueError, OSError):
        pass  # Non-existent path - allow it

    full = (vr / rel).resolve()
    try:
        full.relative_to(vr)
    except ValueError:
        die(f"path escapes vault root: {rel}")
    return str(full.relative_to(vr))


def read(rel: str) -> Dict:
    rel = _safe_rel(rel)
    p = vault_root() / rel
    if not p.exists():
        return {"exists": False, "path": rel}
    return {"exists": True, "path": rel, "content": p.read_text()}


def write(rel: str, content: str, auto_commit: bool = True) -> Dict:
    """Atomic write to vault/<rel>. Commits by default (agent-neutral persist)."""
    rel = _safe_rel(rel)
    p = vault_root() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, p)
    result = {"written": True, "path": rel, "bytes": len(content.encode("utf-8"))}
    if auto_commit:
        result["commit"] = commit()
    return result


def log_append(detail: str, op: str = "-", agent: Optional[str] = None) -> Dict:
    """Prepend a row to wiki/log.md (newest first). Append-only semantics."""
    agent = agent or os.environ.get("WIKI_AGENT", "agent")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    row = f"| {ts} | {agent} | {op} | {detail} |"
    logp = vault_root() / "wiki" / "log.md"
    acquired = lock_acquire("wiki/log.md", timeout=30)
    if not acquired.get("acquired"):
        return {"logged": False, "reason": "lock timeout", "held_by": acquired.get("held_by")}
    try:
        lines = logp.read_text().splitlines() if logp.exists() else ["# Log", "", "| Date | Agent | Op | Detail |", "|------|-------|----|--------|"]
        # insert after the header table separator (first line that is the "|---|" row), else after line 0
        insert_at = 0
        for i, ln in enumerate(lines):
            if re.match(r"^\|[-\s|]+\|\s*$", ln):
                insert_at = i + 1
                break
        lines.insert(insert_at, row)
        logp.write_text("\n".join(lines) + "\n")
    finally:
        lock_release("wiki/log.md")
    return {"logged": True, "row": row}


def hot_get() -> Dict:
    p = vault_root() / "wiki" / "hot.md"
    return {"content": p.read_text() if p.exists() else ""}


def hot_set(content: str) -> Dict:
    return write("wiki/hot.md", content, auto_commit=True) | {"note": "hot cache overwritten"}


def index_get() -> Dict:
    p = vault_root() / "wiki" / "index.md"
    return {"content": p.read_text() if p.exists() else ""}


# ── git (agent-neutral commit, replaces the Claude-only PostToolUse hook) ────
def _git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(vault_root()), *args],
                          capture_output=True, text=True, check=check)


def commit(message: Optional[str] = None) -> Dict:
    """Stage wiki/ .raw/ .vault-meta/ and commit if changed. Skips if locks held."""
    if not (vault_root() / ".git").exists():
        return {"committed": False, "reason": "not a git repo"}
    held = lock_list().get("locks", [])
    live = [l for l in held if not l.get("stale") and not l.get("corrupt")]
    if live:
        return {"committed": False, "reason": "locks held", "count": len(live)}
    msg = message or f"wiki: auto-commit {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    # Add each scope path individually and tolerate empty dirs (git errors on a
    # pathspec that matches no file, which would abort a combined add).
    for path in ("wiki/", ".raw/", ".vault-meta/"):
        if (vault_root() / path.rstrip("/")).exists():
            _git("add", "--", path)
    diff = _git("diff", "--cached", "--quiet")
    if diff.returncode == 0:
        return {"committed": False, "reason": "nothing staged"}
    c = _git("commit", "-m", msg)
    if c.returncode != 0:
        return {"committed": False, "reason": c.stderr.strip() or c.stdout.strip()}
    return {"committed": True, "message": msg}


# ── lint (mechanical checks; semantic fixes stay with the agent) ─────────────
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


def _parse_frontmatter(text: str) -> Dict:
    fm = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for ln in text[3:end].splitlines():
                if ":" in ln:
                    k, _, v = ln.partition(":")
                    fm[k.strip()] = v.strip().strip('"')
    return fm


def lint() -> Dict:
    wiki = vault_root() / "wiki"
    pages = {}  # rel -> {title, type, links[], inbound:int}
    for md in wiki.rglob("*.md"):
        rel = str(md.relative_to(vault_root()))
        text = md.read_text()
        fm = _parse_frontmatter(text)
        targets = []
        for m in WIKILINK.finditer(text):
            tgt = m.group(1).strip()
            if tgt:
                targets.append(tgt)
        pages[rel] = {
            "title": fm.get("title") or md.stem,
            "type": fm.get("type", ""),
            "links": targets,
            "inbound": 0,
            "frontmatter_gaps": [k for k in ("type", "title") if k not in fm],
        }
    # index link names -> page rel (by title or stem)
    names = {}
    for rel, info in pages.items():
        names[info["title"].lower()] = rel
        names[Path(rel).stem.lower()] = rel
    dead, orphans = [], []
    for rel, info in pages.items():
        if Path(rel).name in ("index.md", "log.md", "hot.md"):
            continue
        for tgt in info["links"]:
            if tgt.lower() not in names:
                dead.append({"from": rel, "target": tgt})
            else:
                pages[names[tgt.lower()]]["inbound"] += 1
    for rel, info in pages.items():
        if Path(rel).name in ("index.md", "log.md", "hot.md"):
            continue
        if info["inbound"] == 0:
            orphans.append(rel)
    return {
        "pages": len(pages),
        "dead_links": dead,
        "orphans": orphans,
        "frontmatter_gaps": {r: i["frontmatter_gaps"] for r, i in pages.items()
                             if i["frontmatter_gaps"] and Path(r).name not in ("index.md", "log.md", "hot.md")},
    }


# ── DragonScale address (best-effort; allocate-address.sh needs `flock`) ─────
def address() -> Dict:
    cp = _script("allocate-address.sh", "--peek")
    if cp.returncode != 0:
        return {"available": False, "reason": cp.stderr.strip() or cp.stdout.strip(),
                "hint": "DragonScale addressing needs the `flock` binary (brew install util-linux)."}
    return {"available": True, "next": cp.stdout.strip()}


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--vault", default=os.environ.get("WIKI_VAULT"),
                        help="vault path (default: $WIKI_VAULT)")
    parent.add_argument("--json", action="store_true", help="emit JSON")

    ap = argparse.ArgumentParser(prog="engine", description="claude-obsidian vault engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", parents=[parent], help="vault + engine status")
    p_route = sub.add_parser("route", parents=[parent], help="filing path for new content")
    p_route.add_argument("type", choices=["source", "entity", "concept", "session", "research"])
    p_route.add_argument("name")

    p_lock = sub.add_parser("lock", parents=[parent], help="per-file advisory lock")
    p_lock.add_argument("action", choices=["acquire", "release", "list"])
    p_lock.add_argument("path", nargs="?")
    p_lock.add_argument("--timeout", type=int, default=STALE_AFTER)
    p_lock.add_argument("--force", action="store_true")

    p_w = sub.add_parser("write", parents=[parent], help="write a vault file (atomic; auto-commits)")
    p_w.add_argument("path"); p_w.add_argument("content", nargs="?")
    p_w.add_argument("--no-commit", action="store_true")
    p_w.add_argument("--stdin", action="store_true", help="read content from stdin")

    p_r = sub.add_parser("read", parents=[parent], help="read a vault file")
    p_r.add_argument("path")

    sub.add_parser("lint", parents=[parent], help="mechanical health check")
    sub.add_parser("commit", parents=[parent], help="git stage + commit (skips if locks held)")
    sub.add_parser("address", parents=[parent], help="(DragonScale) next page address")
    p_log = sub.add_parser("log", parents=[parent], help="append a row to wiki/log.md")
    p_log.add_argument("detail"); p_log.add_argument("--op", default="-"); p_log.add_argument("--agent")
    sub.add_parser("hot-get", parents=[parent], help="read hot cache")
    p_hot = sub.add_parser("hot-set", parents=[parent], help="overwrite hot cache")
    p_hot.add_argument("content", nargs="?"); p_hot.add_argument("--stdin", action="store_true")
    sub.add_parser("index", parents=[parent], help="read wiki/index.md")

    args = ap.parse_args(argv)
    if args.vault:
        os.environ["WIKI_VAULT"] = args.vault

    def emit(obj):
        print(json.dumps(obj, indent=2) if args.json else _render(obj, args))

    if args.cmd == "status":
        emit(status())
    elif args.cmd == "route":
        p = route(args.type, args.name)
        print(json.dumps({"path": p}) if args.json else p)
    elif args.cmd == "lock":
        if args.action == "list":
            emit(lock_list())
        elif args.action == "acquire":
            emit(lock_acquire(args.path, args.timeout))
        else:
            emit(lock_release(args.path, force=args.force))
    elif args.cmd == "write":
        content = sys.stdin.read() if args.stdin else args.content
        if content is None:
            die("write requires CONTENT or --stdin")
        emit(write(args.path, content, auto_commit=not args.no_commit))
    elif args.cmd == "read":
        emit(read(args.path))
    elif args.cmd == "lint":
        emit(lint())
    elif args.cmd == "commit":
        emit(commit())
    elif args.cmd == "address":
        emit(address())
    elif args.cmd == "log":
        emit(log_append(args.detail, op=args.op, agent=args.agent))
    elif args.cmd == "hot-get":
        emit(hot_get())
    elif args.cmd == "hot-set":
        content = sys.stdin.read() if args.stdin else args.content
        if content is None:
            die("hot-set requires CONTENT or --stdin")
        emit(hot_set(content))
    elif args.cmd == "index":
        emit(index_get())
    return 0


def _render(obj: Dict, args) -> str:
    """Human-readable fallback when --json is not passed."""
    if args.cmd == "status":
        return (f"vault:   {obj.get('vault_root')}\n"
                f"engine:  {obj.get('engine')}\n"
                f"mode:    {obj.get('mode', '?')}\n"
                f"transport: {obj.get('transport', '?')}\n"
                f"locks:   {obj.get('locks_held', 0)} held\n"
                f"git:     {obj.get('git')}")
    if args.cmd == "read" and obj.get("content") is not None:
        return obj["content"]
    if args.cmd == "index" or args.cmd == "hot-get":
        return obj.get("content", "")
    if args.cmd == "lint":
        return (f"pages: {obj['pages']}\n"
                f"dead links: {len(obj['dead_links'])}\n"
                f"orphans: {len(obj['orphans'])}\n"
                f"frontmatter gaps: {len(obj['frontmatter_gaps'])}")
    return json.dumps(obj, indent=2)


if __name__ == "__main__":
    sys.exit(main())
