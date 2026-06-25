#!/usr/bin/env python3
"""test_engine_safety.py — hermetic tests for engine._safe_rel path validation.

Verifies the security boundary: vault-relative paths are accepted; traversals,
absolute paths, control chars, and symlink escapes are rejected with exit 2.
No network, no LLM, no git. Pure stdlib.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("engine", ROOT / "scripts" / "engine.py")
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


class Fail(SystemExit):
    pass


def assert_eq(label, expected, actual):
    if expected != actual:
        raise Fail(f"FAIL {label}: expected {expected!r}, got {actual!r}")
    print(f"OK   {label}")


def assert_exits(label, code, fn):
    try:
        fn()
    except SystemExit as e:
        assert_eq(f"{label} (exit code)", code, e.code)
        print(f"OK   {label}")
        return
    raise Fail(f"FAIL {label}: expected SystemExit({code}), nothing raised")


PASS = 0


def run(fn):
    global PASS
    fn()
    PASS += 1


with tempfile.TemporaryDirectory() as tmp:
    os.environ["WIKI_VAULT"] = tmp
    (Path(tmp) / "wiki").mkdir(parents=True, exist_ok=True)

    def t_valid():
        assert_eq("valid vault-relative path", "wiki/foo.md",
                  engine._safe_rel("wiki/foo.md"))

    def t_empty():
        assert_exits("empty path rejected", 2, lambda: engine._safe_rel(""))

    def t_absolute():
        assert_exits("absolute path rejected", 2,
                     lambda: engine._safe_rel("/etc/passwd"))

    def t_traversal():
        assert_exits("parent traversal rejected", 2,
                     lambda: engine._safe_rel("../../../etc/passwd"))

    def t_newline():
        assert_exits("newline rejected", 2,
                     lambda: engine._safe_rel("wiki/a\nb.md"))
        assert_exits("carriage return rejected", 2,
                     lambda: engine._safe_rel("wiki/a\rb.md"))

    def t_symlink_escape():
        link = Path(tmp) / "wiki" / "evil"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to("/tmp")  # target outside the vault
        assert_exits("symlink escape rejected", 2,
                     lambda: engine._safe_rel("wiki/evil"))

    for t in (t_valid, t_empty, t_absolute, t_traversal, t_newline, t_symlink_escape):
        run(t)

print(f"\nAll engine safety tests passed. ({PASS})")
