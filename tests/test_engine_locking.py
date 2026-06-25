#!/usr/bin/env python3
"""test_engine_locking.py — hermetic tests for engine locking primitives.

Covers acquire/release round-trip, that a held lock blocks a second acquirer
until it times out, stale-lock reclamation, and lock_list accuracy. Age-based
records live on disk, so blocking/staleness are testable within one process.
No network, no LLM. Pure stdlib.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
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


def assert_true(label, cond, hint=""):
    if not cond:
        raise Fail(f"FAIL {label}{(': ' + hint) if hint else ''}")
    print(f"OK   {label}")


PASS = 0


def run(fn):
    global PASS
    fn()
    PASS += 1


with tempfile.TemporaryDirectory() as tmp:
    os.environ["WIKI_VAULT"] = tmp
    engine._locks_dir  # ensure dir exists (mkdir side effect)

    def t_acquire_release():
        a = engine.lock_acquire("wiki/a.md", timeout=3)
        assert_eq("acquire succeeds", True, a["acquired"])
        r = engine.lock_release("wiki/a.md")
        assert_eq("release succeeds", True, r["released"])

    def t_blocks_when_held():
        engine.lock_acquire("wiki/b.md", timeout=3)
        start = time.time()
        b = engine.lock_acquire("wiki/b.md", timeout=1)
        elapsed = time.time() - start
        assert_eq("second acquire blocked (timed out)", False, b["acquired"])
        assert_true("blocked for ~>=0.9s", elapsed >= 0.9, f"{elapsed:.2f}s")
        engine.lock_release("wiki/b.md")

    def t_reclaim_stale():
        p = "wiki/c.md"
        lf = engine._lockfile(p)
        lf.parent.mkdir(parents=True, exist_ok=True)
        lf.write_text(json.dumps({
            "path": p, "pid": 99999, "host": "dead-host",
            "acquired_at": "2000-01-01T00:00:00Z", "agent": "ghost",
        }))
        a = engine.lock_acquire(p, timeout=1)
        assert_eq("stale lock reclaimed", True, a["acquired"])
        engine.lock_release(p)

    def t_list():
        p = "wiki/d.md"
        engine.lock_acquire(p, timeout=3)
        lst = engine.lock_list()
        assert_eq("list count when held", 1, lst["count"])
        assert_true("list contains path",
                    any(l.get("path") == p for l in lst["locks"]))
        engine.lock_release(p)
        assert_eq("list empty after release", 0, engine.lock_list()["count"])

    for t in (t_acquire_release, t_blocks_when_held, t_reclaim_stale, t_list):
        run(t)

print(f"\nAll engine locking tests passed. ({PASS})")
