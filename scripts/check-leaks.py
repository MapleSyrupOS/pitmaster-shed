#!/usr/bin/env python3
"""check-leaks.py: catch personal details before they are committed or published.

Two layers:
  generic   patterns anyone's repository should not carry: home-folder paths, email addresses, IP addresses,
            and API tokens or private keys. These run everywhere, including CI.
  denylist  a private list of real names, hosts and paths that lives OUTSIDE the repository (by default
            ~/.config/pitmaster/denylist.txt, or the file named by $PITMASTER_DENYLIST). One term per line,
            matched case-insensitively on word boundaries; a line starting with "re:" is a regular expression;
            "#" starts a comment. When no denylist is present, only the generic layer runs.

A match that is fine on purpose goes in scripts/leak-allow.txt: one line per exemption, "path-glob<TAB>regex",
where the regex must match the matched text. Exemptions are public, so they never name a denylist term.

Usage:
  python3 scripts/check-leaks.py             check every file git tracks or would add
  python3 scripts/check-leaks.py --staged    check what is staged (the pre-commit hook uses this)
  python3 scripts/check-leaks.py PATH...     check these files
  python3 scripts/check-leaks.py --self-test

Denylist matches are reported without the term itself, so a CI log or a shared terminal never repeats it.
Exit status: 0 clean, 1 findings, 2 usage error. Python 3.8+, standard library only.
"""
from __future__ import annotations

import fnmatch
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from typing import Iterable, List, Optional, Tuple

ROOT = pathlib.Path(__file__).resolve().parent.parent
ALLOW_FILE = ROOT / "scripts" / "leak-allow.txt"
DEFAULT_DENYLIST = pathlib.Path.home() / ".config" / "pitmaster" / "denylist.txt"

GENERIC: List[Tuple[str, "re.Pattern[str]"]] = [
    ("home path", re.compile(r"(?:/Users/|/home/)(?!runner\b|<|\$|\{)[A-Za-z0-9._-]+|[A-Za-z]:\\Users\\[A-Za-z0-9._-]+")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")),
    ("IP address", re.compile(r"(?<![\d.§])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])")),
    ("token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"
        r"|AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# Matches that are never personal: documentation addresses, loopback, and placeholders.
BENIGN = [
    re.compile(r"@(?:example\.(?:com|org|net)|users\.noreply\.github\.com|anthropic\.com)$", re.I),
    re.compile(r"^(?:127\.0\.0\.1|0\.0\.0\.0|255\.255\.255\.\d+|192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+)$"),
]
VERSIONISH = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,3}\.\d{1,3}$")


def load_denylist(path: Optional[pathlib.Path]) -> List["re.Pattern[str]"]:
    if path is None or not path.is_file():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            out.append(re.compile(line[3:], re.I))
        else:
            out.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(line) + r"(?![A-Za-z0-9])", re.I))
    return out


def load_allow(path: pathlib.Path) -> List[Tuple[str, "re.Pattern[str]"]]:
    if not path.is_file():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        glob, _, rx = raw.partition("\t")
        out.append((glob.strip(), re.compile(rx.strip())))
    return out


def is_benign(kind: str, text: str) -> bool:
    if kind == "email":
        return bool(BENIGN[0].search(text))
    if kind == "IP address":
        return bool(BENIGN[1].match(text)) or bool(VERSIONISH.match(text) and text.startswith(("0.", "1.", "2.", "3.")))
    return False


def scan_text(rel: str, text: str, deny, allow) -> List[str]:
    findings = []
    for n, line in enumerate(text.splitlines(), start=1):
        for kind, rx in GENERIC:
            for m in rx.finditer(line):
                hit = m.group(0)
                if is_benign(kind, hit):
                    continue
                if any(fnmatch.fnmatch(rel, g) and a.search(hit) for g, a in allow):
                    continue
                findings.append(f"{rel}:{n}: {kind}: {hit}")
        for rx in deny:
            if rx.search(line):
                findings.append(f"{rel}:{n}: denylist term (not shown)")
    return findings


def git(*args: str) -> List[str]:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return [l for l in r.stdout.split("\0" if "-z" in args else "\n") if l]


def is_text(data: bytes) -> bool:
    return b"\0" not in data[:8192]


def iter_targets(mode: str, paths: List[str]) -> Iterable[Tuple[str, bytes]]:
    if mode == "staged":
        for rel in git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"):
            r = subprocess.run(["git", "-C", str(ROOT), "show", f":{rel}"], capture_output=True)
            yield rel, r.stdout
    elif mode == "paths":
        for p in paths:
            fp = pathlib.Path(p)
            yield str(fp), fp.read_bytes()
    else:
        files = set(git("ls-files", "-z")) | set(git("ls-files", "--others", "--exclude-standard", "-z"))
        for rel in sorted(files):
            fp = ROOT / rel
            if fp.is_file():
                yield rel, fp.read_bytes()


def run(mode: str, paths: List[str], denylist: Optional[pathlib.Path], quiet: bool = False) -> int:
    deny = load_denylist(denylist)
    allow = load_allow(ALLOW_FILE)
    findings: List[str] = []
    count = 0
    for rel, data in iter_targets(mode, paths):
        if rel.replace("\\", "/") == "scripts/check-leaks.py" or not is_text(data):
            continue
        count += 1
        findings += scan_text(rel, data.decode("utf-8", errors="replace"), deny, allow)
    if not quiet:
        for f in findings:
            print(f)
        layer = "generic + denylist" if deny else "generic only (no denylist found)"
        print(f"check-leaks: {count} files, {len(findings)} finding(s), {layer}")
    return 1 if findings else 0


def self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        deny = d / "deny.txt"
        deny.write_text("# test\nzorblax\nre:\\bqu+x\\b\n")
        clean = d / "clean.md"
        clean.write_text("Alex and Sam ran it on alpha. See https://example.com and noreply@example.com.\n"
                         "Version 1.2.3.4 is fine, and so is 127.0.0.1 and 192.0.2.7.\n")
        assert run("paths", [str(clean)], deny, quiet=True) == 0, "clean file must pass"
        reds = {
            "a home path": "see /Users/someone/Documents/x\n",
            "an email": "write to someone@realmail.net\n",
            "an IP": "ssh to 88.99.12.34 today\n",
            "a token": "token ghp_" + "a" * 36 + "\n",
            "a private key": "-----BEGIN OPENSSH PRIVATE KEY-----\n",
            "a denylist word": "Ask Zorblax first.\n",
            "a denylist regex": "the quuux box\n",
            "an IP after a word": "host at 10.20.30.40\n",
        }
        for why, text in reds.items():
            f = d / "red.md"
            f.write_text(text)
            assert run("paths", [str(f)], deny, quiet=True) == 1, f"red arm did not fail: {why}"
        assert scan_text("x", "see §27.2.5.3 of the spec", [], []) == [], "a section number is not an IP"
        assert scan_text("x", "zorblaxian", load_denylist(deny), []) == [], "word boundary must hold"
        assert scan_text("x", "zorblax's", load_denylist(deny), []) != [], "possessive must match"
        out = scan_text("x", "Zorblax", load_denylist(deny), [])
        assert out and "zorblax" not in out[0].lower(), "a denylist hit must not print the term"
    print("check-leaks: self-test passed")
    return 0


def main(argv: List[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    env = os.environ.get("PITMASTER_DENYLIST")
    denylist = pathlib.Path(env).expanduser() if env else DEFAULT_DENYLIST
    if "--staged" in argv:
        return run("staged", [], denylist)
    paths = [a for a in argv if not a.startswith("--")]
    if paths:
        return run("paths", paths, denylist)
    return run("all", [], denylist)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
