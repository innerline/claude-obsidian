#!/usr/bin/env python3
"""test_mcp_server.py — hermetic tests for the agentloop MCP server.

Requires the `mcp` SDK — run via:
    uv run --with mcp python3 tests/test_mcp_server.py

Verifies all 13 wiki_* tools are registered, that tools delegate to engine
primitives, and that wiki_write performs the lock -> write -> release -> commit
cycle. No external network.
"""
import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Fail(SystemExit):
    pass


def assert_eq(label, expected, actual):
    if expected != actual:
        raise Fail(f"FAIL {label}: expected {expected!r}, got {actual!r}")
    print(f"OK   {label}")


def assert_true(label, cond, hint=""):
    if not cond:
        raise Fail(f"FAIL {label}{(': ' + hint) if hint else ''}")
    print(f"OK   {label}")


EXPECTED = {
    "wiki_status", "wiki_route", "wiki_lock_list", "wiki_lock_acquire",
    "wiki_lock_release", "wiki_write", "wiki_read", "wiki_log",
    "wiki_hot_get", "wiki_hot_set", "wiki_index", "wiki_lint", "wiki_commit",
}

PASS = 0


def run(fn):
    global PASS
    fn()
    PASS += 1


with tempfile.TemporaryDirectory() as tmp:
    os.environ["WIKI_VAULT"] = tmp
    (Path(tmp) / "wiki").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", tmp], check=True)
    subprocess.run(["git", "-C", tmp, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", tmp, "config", "user.name", "t"], check=True)

    spec = importlib.util.spec_from_file_location(
        "mcp_server", ROOT / "scripts" / "mcp_server.py")
    ms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ms)

    def t_tools_registered():
        tools = asyncio.run(ms.mcp.list_tools())
        names = {t.name for t in tools}
        assert_eq("13 wiki_* tools registered", EXPECTED, names)

    def t_status_delegates():
        r = ms.wiki_status()
        assert_true("status returns vault_root", "vault_root" in r)
        assert_true("status includes mode or transport",
                    "mode" in r or "transport" in r)

    def t_route_delegates():
        p = ms.wiki_route("concept", "Foo")
        assert_true("route returns a .md path",
                    isinstance(p, str) and p.endswith(".md"))

    def t_write_cycle():
        content = "---\ntype: concept\ntitle: Foo\n---\n# Foo\n"
        r = ms.wiki_write("wiki/foo.md", content)
        assert_eq("write succeeded", True, r.get("written"))
        assert_true("file landed on disk",
                    (Path(tmp) / "wiki" / "foo.md").exists())
        assert_eq("no locks left after write",
                  0, ms.wiki_lock_list()["count"])

    def t_lint_delegates():
        r = ms.wiki_lint()
        assert_true("lint returns pages count", "pages" in r)

    for t in (t_tools_registered, t_status_delegates, t_route_delegates,
              t_write_cycle, t_lint_delegates):
        run(t)

print(f"\nAll MCP server tests passed. ({PASS})")
