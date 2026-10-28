#!/usr/bin/env python3
"""run-self-tests.py: run every shed tool's self-test from a lone copy of its folder.

Usage: run-self-tests.py [TOOL ...]

A tool is a top-level folder <name>/ holding <name>/<name>.py. For each one, the folder is
copied alone into an empty temporary directory outside any repository, and
``python <name>.py --self-test`` runs there. This is the copy-one-folder test: a tool must
work with nothing but its own folder. With TOOL names, only those run.

Exit 0 when every self-test passes, 1 when any fails, 2 when a named tool does not exist.
Python 3.8+, standard library only.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent


def find_tools() -> List[str]:
    return sorted(p.name for p in ROOT.iterdir() if p.is_dir() and (p / f"{p.name}.py").is_file())


def main(argv: List[str]) -> int:
    tools = find_tools()
    wanted = argv or tools
    missing = [t for t in wanted if t not in tools]
    if missing:
        print(f"run-self-tests: no such tool: {', '.join(missing)}")
        return 2
    failed = []
    for name in wanted:
        with tempfile.TemporaryDirectory() as td:
            solo = Path(td) / name
            shutil.copytree(ROOT / name, solo)
            print(f"::group::{name}", flush=True)
            r = subprocess.run([sys.executable, f"{name}.py", "--self-test"], cwd=solo)
            print("::endgroup::", flush=True)
            print(f"run-self-tests: {name} -- {'PASS' if r.returncode == 0 else f'FAIL (exit {r.returncode})'}")
            if r.returncode != 0:
                failed.append(name)
    if failed:
        print(f"run-self-tests: FAIL -- {len(failed)} of {len(wanted)} tool(s): {', '.join(failed)}")
        return 1
    print(f"run-self-tests: PASS -- {len(wanted)} tool(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
