#!/usr/bin/env python3
"""test_engine_commit.py — hermetic tests for engine.commit().

Verifies the agent-neutral commit: commits when there are staged-scope changes,
skips when nothing changed, skips while locks are held, and honors a custom
message. Each test uses a fresh throwaway git repo. No network, no LLM.
"""
import importlib.util
import os
import subprocess
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


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


def fresh_vault():
    tmp = tempfile.mkdtemp(prefix="engine-commit-")
    os.environ["WIKI_VAULT"] = tmp
    _git(tmp, "init", "-q")
    _git(tmp, "config", "user.email", "test@example.com")
    _git(tmp, "config", "user.name", "Test")
    (Path(tmp) / "wiki").mkdir()
    return tmp


PASS = 0


def run(fn):
    global PASS
    fn()
    PASS += 1


def t_commits_changes():
    tmp = fresh_vault()
    (Path(tmp) / "wiki" / "a.md").write_text("# A")
    r = engine.commit()
    assert_eq("commits when changes present", True, r["committed"])


def t_nothing_staged():
    tmp = fresh_vault()
    (Path(tmp) / "wiki" / "a.md").write_text("# A")
    engine.commit()
    r = engine.commit()
    assert_eq("nothing to commit -> False", False, r["committed"])
    assert_eq("reason is nothing staged", "nothing staged", r["reason"])


def t_skips_when_locked():
    tmp = fresh_vault()
    engine.lock_acquire("wiki/b.md", timeout=3)
    (Path(tmp) / "wiki" / "b.md").write_text("# B")
    r = engine.commit()
    assert_eq("skip while locked -> False", False, r["committed"])
    assert_eq("reason is locks held", "locks held", r["reason"])
    engine.lock_release("wiki/b.md")
    r2 = engine.commit()
    assert_eq("commits after release", True, r2["committed"])


def t_custom_message():
    tmp = fresh_vault()
    (Path(tmp) / "wiki" / "c.md").write_text("# C")
    r = engine.commit(message="custom: my message")
    assert_eq("custom commit succeeds", True, r["committed"])
    assert_eq("custom message recorded", "custom: my message", r["message"])


for t in (t_commits_changes, t_nothing_staged, t_skips_when_locked, t_custom_message):
    run(t)

print(f"\nAll engine commit tests passed. ({PASS})")
