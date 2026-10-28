#!/usr/bin/env python3
"""comment-only: prove that an edit changed only comments.

Usage: comment-only.py [--rev REV] [--to REV] [--c-ext EXT] [--asm-ext EXT] [PATH ...]
       comment-only.py --self-test

What it does
    Strips comments and blank lines from each file at two points (a git
    revision, or the working tree) and compares what is left. Identical output
    means no code changed: the edit touched only comments, blank lines or
    whitespace between tokens.

        comment-only.py src/main.cpp include/util.h
                                       # HEAD against the working tree
        comment-only.py --rev HEAD~1 --to HEAD
                                       # a commit, every file it changed

    With no paths, the files are those ``git diff --name-only`` lists between
    the two points; any it cannot read by extension are named in the verdict
    line as "not checked", never counted as passing. Run it from inside the
    repository; paths are relative to the current directory.

What it promises
    - Exit 0: every file's code is identical at the two points.
    - Exit 1: at least one file's code differs (the first differing line is
      printed), or a file is missing on one side.
    - Exit 2: a usage error, a file whose extension is not supported, or git
      could not answer (not a repository, unknown revision, no git).
    - It reports only. It never edits, creates or deletes a file.

Supported languages (the extension decides)
    - C and C++ (.c .cc .cpp .h .hpp .inc) and linker scripts (.ld): ``//``
      and ``/* */`` comments. String and character literals are code,
      including raw strings (``R"x(...)x"``), and a digit separator
      (``0x9E37'79B9``) is part of its number, not a character literal.
    - NASM assembly (.asm): ``;`` comments. Strings in '', "" and `` are code.
    --c-ext EXT and --asm-ext EXT (each may repeat) add an extension to a
    language, or move one: ``--asm-ext .inc`` reads .inc files as assembly.

Whitespace runs inside code collapse to one space and each line is trimmed,
so re-indenting code reads as identical. Whitespace inside a literal is kept
exactly. A literal left open at the end of a line (an apostrophe in an
``#error`` message) ends there; any comment marker after it on that line is
then kept as code, which can only make the check fail, never pass wrongly.

--self-test
    Runs the built-in cases, prints one PASS, FAIL or SKIP line per case and a
    verdict line, and exits non-zero on any failure. The cases that need git
    are skipped, and counted, when git is not found.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

PROG = "comment-only"
DEFAULT_EXTS: Dict[str, str] = {
    ".c": "c", ".cc": "c", ".cpp": "c", ".h": "c", ".hpp": "c", ".inc": "c", ".ld": "c",
    ".asm": "asm",
}
IDENT = re.compile(r"[A-Za-z0-9_]")
RAW_PREFIX = re.compile(r"(?:u8|[uUL])?R$")


class Unsupported(Exception):
    """The file's extension has no comment grammar here."""


class GitError(Exception):
    """git could not answer."""


def _norm_ext(ext: str) -> str:
    ext = ext.strip().lower()
    return ext if ext.startswith(".") else "." + ext


def language(path: str, exts: Optional[Dict[str, str]] = None) -> str:
    """Return "c" or "asm" for PATH, or raise Unsupported."""
    table = DEFAULT_EXTS if exts is None else exts
    ext = Path(path).suffix.lower()
    if ext in table:
        return table[ext]
    raise Unsupported(f"{path}: unsupported extension '{ext}'")


def _quoted(text: str, i: int, quote: str, allow_escape: bool) -> int:
    """Return the index just past the literal opened at TEXT[i].

    The literal ends at its closing QUOTE or, if none comes first, at the end
    of the line.
    """
    j = i + 1
    n = len(text)
    while j < n:
        ch = text[j]
        if allow_escape and ch == "\\" and j + 1 < n and text[j + 1] != "\n":
            j += 2
            continue
        if ch == quote:
            return j + 1
        if ch == "\n":
            return j
        j += 1
    return j


def _raw_string(text: str, i: int) -> int:
    """Return the index just past the raw string whose quote is TEXT[i]."""
    open_paren = text.find("(", i + 1)
    if open_paren < 0:
        return len(text)
    delim = text[i + 1:open_paren]
    close = text.find(")" + delim + '"', open_paren + 1)
    if close < 0:
        return len(text)
    return close + len(delim) + 2


def _pp_number(text: str, i: int) -> int:
    """Return the index just past the preprocessing number at TEXT[i]."""
    j = i + 1
    n = len(text)
    while j < n:
        ch = text[j]
        if ch in "+-" and text[j - 1] in "eEpP":
            j += 1
        elif IDENT.match(ch) or ch == ".":
            j += 1
        elif ch == "'" and j + 1 < n and IDENT.match(text[j + 1]):
            j += 2
        else:
            break
    return j


def segments_c(text: str) -> List[Tuple[str, str]]:
    """Split C/C++ TEXT into ("code" | "lit" | "comment", text) segments."""
    out: List[Tuple[str, str]] = []
    code_start = 0
    i = 0
    n = len(text)

    def flush(upto: int) -> None:
        if upto > code_start:
            out.append(("code", text[code_start:upto]))

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            flush(i)
            j = i + 2
            while j < n and not (text[j] == "\n" and text[j - 1] != "\\"):
                j += 1
            out.append(("comment", text[i:j]))
            i = code_start = j
        elif ch == "/" and nxt == "*":
            flush(i)
            end = text.find("*/", i + 2)
            j = n if end < 0 else end + 2
            out.append(("comment", text[i:j]))
            i = code_start = j
        elif ch == '"':
            word = re.search(r"[A-Za-z0-9_]*$", text[code_start:i])
            prefix = word.group(0) if word else ""
            start = i - len(prefix) if RAW_PREFIX.search(prefix) else i
            j = _raw_string(text, i) if start != i else _quoted(text, i, '"', True)
            flush(start)
            out.append(("lit", text[start:j]))
            i = code_start = j
        elif ch == "'":
            flush(i)
            j = _quoted(text, i, "'", True)
            out.append(("lit", text[i:j]))
            i = code_start = j
        elif ch.isdigit() and (i == 0 or not IDENT.match(text[i - 1])):
            i = _pp_number(text, i)
        elif IDENT.match(ch):
            while i < n and IDENT.match(text[i]):
                i += 1
        else:
            i += 1
    flush(n)
    return out


def segments_asm(text: str) -> List[Tuple[str, str]]:
    """Split NASM TEXT into ("code" | "lit" | "comment", text) segments."""
    out: List[Tuple[str, str]] = []
    code_start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == ";":
            if i > code_start:
                out.append(("code", text[code_start:i]))
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(("comment", text[i:j]))
            i = code_start = j
        elif ch in "'\"`":
            if i > code_start:
                out.append(("code", text[code_start:i]))
            j = _quoted(text, i, ch, ch == "`")
            out.append(("lit", text[i:j]))
            i = code_start = j
        else:
            i += 1
    if n > code_start:
        out.append(("code", text[code_start:n]))
    return out


def strip_comments(text: str, lang: str) -> List[str]:
    """Return TEXT's code lines with comments, blank lines and spacing removed."""
    segs = segments_c(text) if lang == "c" else segments_asm(text)
    parts: List[str] = []
    for kind, body in segs:
        if kind == "comment":
            parts.append(" " + "\n" * body.count("\n"))
        elif kind == "code":
            parts.append(re.sub(r"[ \t\r\f\v]+", " ", body))
        else:
            parts.append(body)
    lines = [line.strip() for line in "".join(parts).split("\n")]
    return [line for line in lines if line]


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def _git(args: List[str]) -> "subprocess.CompletedProcess[bytes]":
    try:
        return subprocess.run(["git"] + args, capture_output=True, check=False)
    except OSError as exc:
        raise GitError(f"cannot run git ({exc})") from exc


def _git_path(path: str) -> str:
    """Spell PATH the way ``git show REV:PATH`` wants it: relative to the current directory."""
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.relative_to(Path.cwd())
        except ValueError:
            return p.as_posix()
    return "./" + p.as_posix()


def read_at(rev: Optional[str], path: str) -> Optional[str]:
    """Return PATH's text at REV (None means the working tree), None if absent."""
    if rev is None:
        p = Path(path)
        return _decode(p.read_bytes()) if p.is_file() else None
    proc = _git(["show", f"{rev}:{_git_path(path)}"])
    if proc.returncode != 0:
        return None
    return _decode(proc.stdout)


def compare(path: str, old: Optional[str], new: Optional[str],
            exts: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Return None when the code is identical, else a one-line reason."""
    if old is None or new is None:
        side = "old" if old is None else "new"
        return f"{path}: missing on the {side} side"
    lang = language(path, exts)
    a = strip_comments(old, lang)
    b = strip_comments(new, lang)
    if a == b:
        return None
    for k, (x, y) in enumerate(zip(a, b), start=1):
        if x != y:
            return f"{path}: code line {k} differs: -{x!r} +{y!r}"
    k = min(len(a), len(b)) + 1
    return f"{path}: code line count differs ({len(a)} -> {len(b)}) at line {k}"


def check_rev(rev: str) -> None:
    proc = _git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"])
    if proc.returncode != 0:
        raise GitError(f"'{rev}' is not a revision git knows here (is this a git repository?)")


def changed_paths(rev: str, to: Optional[str]) -> List[str]:
    """Return the files git lists as changed between REV and TO, relative to the current directory."""
    cmd = ["diff", "--name-only", "--relative", "-z", rev] + ([] if to is None else [to])
    proc = _git(cmd)
    if proc.returncode != 0:
        raise GitError("git diff failed: " + proc.stderr.decode("utf-8", "replace").strip())
    return [_decode(p) for p in proc.stdout.split(b"\0") if p]


def main(argv: List[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(prog=PROG, description="Prove an edit changed only comments.")
    ap.add_argument("--rev", default="HEAD", metavar="REV", help="old side: a git revision (default HEAD)")
    ap.add_argument("--to", default=None, metavar="REV",
                    help="new side: a git revision (default: the working tree)")
    ap.add_argument("--c-ext", action="append", default=[], metavar="EXT",
                    help="read this extension as C/C++ (may repeat)")
    ap.add_argument("--asm-ext", action="append", default=[], metavar="EXT",
                    help="read this extension as NASM assembly (may repeat)")
    ap.add_argument("--self-test", action="store_true", help="run the built-in cases and exit")
    ap.add_argument("paths", nargs="*", metavar="PATH")
    args = ap.parse_args(argv)
    if args.self_test:
        return _run_self_test()

    exts = dict(DEFAULT_EXTS)
    for e in args.c_ext:
        exts[_norm_ext(e)] = "c"
    for e in args.asm_ext:
        exts[_norm_ext(e)] = "asm"

    paths: List[str] = list(args.paths)
    skipped: List[str] = []
    failures: List[str] = []
    try:
        check_rev(args.rev)
        if args.to is not None:
            check_rev(args.to)
        if not paths:
            for path in changed_paths(args.rev, args.to):
                try:
                    language(path, exts)
                    paths.append(path)
                except Unsupported:
                    skipped.append(path)
        for path in paths:
            reason = compare(path, read_at(args.rev, path), read_at(args.to, path), exts)
            if reason is not None:
                failures.append(reason)
    except Unsupported as err:
        print(f"{PROG}: ERROR -- {err}", file=sys.stderr)
        return 2
    except GitError as err:
        print(f"{PROG}: ERROR -- {err}", file=sys.stderr)
        return 2
    for reason in failures:
        print(f"  {reason}")
    tail = f"; {len(skipped)} not checked: {' '.join(skipped)}" if skipped else ""
    if failures:
        print(f"{PROG}: FAIL -- {len(failures)} of {len(paths)} file(s) changed code{tail}")
        return 1
    print(f"{PROG}: PASS -- {len(paths)} file(s), code identical{tail}")
    return 0


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

# (name, path, old text, new text, are they identical once comments are stripped?)
_ARMS: List[Tuple[str, str, str, str, bool]] = [
    ("reworded line comment", "a.cpp",
     "int x = 1; // old words\n", "int x = 1; // new words here\n", True),
    ("block comment reflowed", "a.cpp",
     "/* one\n two */\nint y;\n", "/*\n * one two\n * three\n */\nint y;\n", True),
    ("comment line deleted", "a.h",
     "// gone\nint z;\n", "int z;\n", True),
    ("re-indented code", "a.cpp",
     "if (a)  {\n  b();\n}\n", "if (a) {\n\tb();\n}\n", True),
    ("line endings are not code", "a.cpp",
     "int x;\nint y; // c\n", "int x;\r\nint y; // d\r\n", True),
    ("code value changed", "a.cpp",
     "int x = 1; // c\n", "int x = 2; // c\n", False),
    ("comment turned into code", "a.cpp",
     "// x = 1;\n", "x = 1;\n", False),
    ("code line added", "a.cpp",
     "int x;\n", "int x;\nint y;\n", False),
    ("slashes inside a string are code", "a.cpp",
     'f("a // b");\n', 'f("a // c");\n', False),
    ("block opener inside a string is code", "a.cpp",
     'f("/*"); g(); f("*/");\n', 'f("/*"); h(); f("*/");\n', False),
    ("whitespace inside a string is code", "a.cpp",
     'f("a  b");\n', 'f("a b");\n', False),
    ("digit separator is not a char literal", "a.cpp",
     "u64 k = 0x9E37'79B9; // old\n", "u64 k = 0x9E37'79B9; // new\n", True),
    ("digit separator value changed", "a.cpp",
     "u64 k = 0x9E37'79B9; // c\n", "u64 k = 0x9E37'79BA; // c\n", False),
    ("raw string body is code", "a.cpp",
     'auto s = R"x(a /* b */ c)x";\n', 'auto s = R"x(a /* B */ c)x";\n', False),
    ("raw string with a quote and slashes is code", "a.cpp",
     'auto s = R"x(a"b // c)x";\n', 'auto s = R"x(a"b // d)x";\n', False),
    ("char literal slash is code", "a.cpp",
     "if (c == '/') f(); // x\n", "if (c == '*') f(); // x\n", False),
    ("apostrophe in #error ends at the line", "a.h",
     "#error don't // keep\nint q;\n", "#error don't // KEEP\nint q;\n", False),
    ("asm comment reworded", "a.asm",
     "mov ax, 1 ; old\n", "mov ax, 1   ; new text\n", True),
    ("asm semicolon in a string is code", "a.asm",
     "db ';', 0\n", "db ';', 1\n", False),
    ("asm code changed", "a.asm",
     "mov ax, 1 ; c\n", "mov ax, 2 ; c\n", False),
]


def _arm_case(path: str, old: str, new: str, want_same: bool) -> Callable[[Path], bool]:
    def case(td: Path) -> bool:
        return (compare(path, old, new) is None) == want_same
    return case


def _case_unsupported_extension(td: Path) -> bool:
    # RED ARM: an extension with no grammar is refused, never read as passing.
    try:
        language("a.sh")
    except Unsupported:
        return True
    return False


def _run(cwd: Path, *args: str) -> Tuple[str, str, int]:
    script = str(Path(__file__).resolve())
    r = subprocess.run([sys.executable, script, *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return r.stdout, r.stderr, r.returncode


def _g(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
         "-c", "core.autocrlf=false", *args],
        cwd=str(repo), check=True, capture_output=True)


def _repo(td: Path) -> Path:
    """A repository with one commit: a.cpp, and an unsupported notes.txt."""
    repo = td / "repo"
    repo.mkdir()
    (repo / "a.cpp").write_bytes(b"int x = 1; // old\nint y = 2;\n")
    (repo / "notes.txt").write_bytes(b"one\n")
    _g(repo, "init", "-q")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "first")
    return repo


def _case_paths_comment_edit_passes(td: Path) -> bool:
    repo = _repo(td)
    (repo / "a.cpp").write_bytes(b"int x = 1; // reworded at length\n\nint y = 2;\n")
    out, _, rc = _run(repo, "a.cpp")
    return rc == 0 and "comment-only: PASS -- 1 file(s), code identical" in out


def _case_paths_code_edit_fails_by_name(td: Path) -> bool:
    # RED ARM: a code edit exits 1 and names the file and the first differing line.
    repo = _repo(td)
    (repo / "a.cpp").write_bytes(b"int x = 1; // old\nint y = 3;\n")
    out, _, rc = _run(repo, "a.cpp")
    return rc == 1 and "a.cpp: code line 2 differs" in out and "FAIL -- 1 of 1 file(s)" in out


def _case_added_code_line_fails(td: Path) -> bool:
    # RED ARM: a changed line count is reported, not just a changed line.
    repo = _repo(td)
    (repo / "a.cpp").write_bytes(b"int x = 1; // old\nint y = 2;\nint z;\n")
    out, _, rc = _run(repo, "a.cpp")
    return rc == 1 and "code line count differs (2 -> 3) at line 3" in out


def _case_no_paths_lists_changed_files(td: Path) -> bool:
    repo = _repo(td)
    (repo / "a.cpp").write_bytes(b"int x = 1; // new words\nint y = 2;\n")
    (repo / "notes.txt").write_bytes(b"two\n")
    out, _, rc = _run(repo)
    return rc == 0 and "1 file(s), code identical; 1 not checked: notes.txt" in out


def _case_no_paths_code_edit_fails(td: Path) -> bool:
    # RED ARM: with no paths, a code edit in a changed file still fails by name.
    repo = _repo(td)
    (repo / "a.cpp").write_bytes(b"int x = 9; // old\nint y = 2;\n")
    out, _, rc = _run(repo)
    return rc == 1 and "a.cpp: code line 1 differs" in out


def _case_unsupported_named_path_exits_2(td: Path) -> bool:
    # RED ARM: a named file with no grammar exits 2 and says so; it is never a pass.
    repo = _repo(td)
    _, err, rc = _run(repo, "notes.txt")
    return rc == 2 and "unsupported extension '.txt'" in err


def _case_rev_to_range(td: Path) -> bool:
    repo = _repo(td)
    (repo / "a.cpp").write_bytes(b"// header\nint x = 1;\nint y = 2;\n")
    _g(repo, "commit", "-q", "-a", "-m", "comment only")
    ok_out, _, ok_rc = _run(repo, "--rev", "HEAD~1", "--to", "HEAD")
    (repo / "a.cpp").write_bytes(b"// header\nint x = 5;\nint y = 2;\n")
    _g(repo, "commit", "-q", "-a", "-m", "code")
    bad_out, _, bad_rc = _run(repo, "--rev", "HEAD~1", "--to", "HEAD")
    # the working tree is put back to the comment-only text: --to must still be read from the commit
    (repo / "a.cpp").write_bytes(b"// header\nint x = 1;\nint y = 2;\n")
    _, _, to_rc = _run(repo, "--rev", "HEAD~1", "--to", "HEAD")
    return (ok_rc == 0 and "PASS -- 1 file(s)" in ok_out
            and bad_rc == 1 and "a.cpp: code line 1 differs" in bad_out and to_rc == 1)


def _case_deleted_file_fails(td: Path) -> bool:
    # RED ARM: a file gone from the new side is a failure, not a pass.
    repo = _repo(td)
    (repo / "a.cpp").unlink()
    out, _, rc = _run(repo, "a.cpp")
    return rc == 1 and "a.cpp: missing on the new side" in out


def _case_bad_rev_exits_2(td: Path) -> bool:
    # RED ARM: an unknown revision exits 2 instead of reading every file as missing.
    repo = _repo(td)
    _, err, rc = _run(repo, "--rev", "no-such-rev", "a.cpp")
    return rc == 2 and "'no-such-rev' is not a revision" in err


def _case_not_a_repository_exits_2(td: Path) -> bool:
    plain = td / "plain"
    plain.mkdir()
    (plain / "a.cpp").write_bytes(b"int x;\n")
    # an ancestor of the temp directory could be a repository on an odd machine; pin the ceiling
    script = str(Path(__file__).resolve())
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(td))
    r = subprocess.run([sys.executable, script, "a.cpp"], cwd=str(plain), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env)
    return r.returncode == 2 and "ERROR" in r.stderr


def _case_run_from_subdirectory(td: Path) -> bool:
    repo = _repo(td)
    sub = repo / "src"
    sub.mkdir()
    (sub / "b.cpp").write_bytes(b"int b; // one\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "sub")
    (sub / "b.cpp").write_bytes(b"int b; // two\n")
    named, _, rc1 = _run(sub, "b.cpp")
    listed, _, rc2 = _run(sub)
    return (rc1 == 0 and "PASS -- 1 file(s)" in named
            and rc2 == 0 and "PASS -- 1 file(s)" in listed)


def _case_ext_flag_moves_an_extension(td: Path) -> bool:
    # .inc is C by default; --asm-ext .inc reads the same file as assembly.
    repo = td / "repo"
    repo.mkdir()
    (repo / "k.inc").write_bytes(b"mov ax, 1 ; old\n")
    _g(repo, "init", "-q")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "first")
    (repo / "k.inc").write_bytes(b"mov ax, 1 ; new\n")
    as_c, _, rc_c = _run(repo, "k.inc")  # ';' is code in C: the edit is a code edit
    as_asm, _, rc_a = _run(repo, "--asm-ext", "inc", "k.inc")
    return rc_c == 1 and "k.inc: code line 1 differs" in as_c and rc_a == 0 and "PASS" in as_asm


def _case_ext_flag_adds_an_extension(td: Path) -> bool:
    repo = td / "repo"
    repo.mkdir()
    (repo / "m.cxx").write_bytes(b"int a; // old\n")
    _g(repo, "init", "-q")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "first")
    (repo / "m.cxx").write_bytes(b"int a; // new\n")
    _, err, rc_no = _run(repo, "m.cxx")
    out, _, rc_yes = _run(repo, "--c-ext", ".cxx", "m.cxx")
    return rc_no == 2 and "unsupported extension '.cxx'" in err and rc_yes == 0 and "PASS" in out


def _case_never_edits(td: Path) -> bool:
    repo = _repo(td)
    body = b"int x = 7; // old\nint y = 2;\n"
    (repo / "a.cpp").write_bytes(body)

    def tree() -> List[Tuple[str, bytes]]:
        return sorted((p.relative_to(repo).as_posix(), p.read_bytes())
                      for p in repo.rglob("*") if p.is_file() and ".git" not in p.relative_to(repo).parts)

    before = tree()
    _run(repo, "a.cpp")
    _run(repo)
    return tree() == before


# (name, function, needs git)
_CASES: List[Tuple[str, Callable[[Path], bool], bool]] = (
    [(f"strip: {name}", _arm_case(path, old, new, same), False) for name, path, old, new, same in _ARMS]
    + [
        ("unsupported-extension-refused", _case_unsupported_extension, False),
        ("paths-comment-edit-passes", _case_paths_comment_edit_passes, True),
        ("paths-code-edit-fails-by-name", _case_paths_code_edit_fails_by_name, True),
        ("added-code-line-fails", _case_added_code_line_fails, True),
        ("no-paths-lists-changed-files", _case_no_paths_lists_changed_files, True),
        ("no-paths-code-edit-fails", _case_no_paths_code_edit_fails, True),
        ("unsupported-named-path-exits-2", _case_unsupported_named_path_exits_2, True),
        ("rev-to-range", _case_rev_to_range, True),
        ("deleted-file-fails", _case_deleted_file_fails, True),
        ("bad-rev-exits-2", _case_bad_rev_exits_2, True),
        ("not-a-repository-exits-2", _case_not_a_repository_exits_2, True),
        ("run-from-subdirectory", _case_run_from_subdirectory, True),
        ("ext-flag-moves-an-extension", _case_ext_flag_moves_an_extension, True),
        ("ext-flag-adds-an-extension", _case_ext_flag_adds_an_extension, True),
        ("never-edits", _case_never_edits, True),
    ]
)


def _run_self_test() -> int:
    """Run every case in its own temporary directory; print PASS/FAIL/SKIP per case."""
    git_ok = shutil.which("git") is not None
    n_pass = n_fail = n_skip = 0
    for name, fn, needs_git in _CASES:
        if needs_git and not git_ok:
            print(f"SKIP {name} (git not found)")
            n_skip += 1
            continue
        with tempfile.TemporaryDirectory() as td:
            try:
                ok = fn(Path(td))
            except Exception as exc:  # a crashing case is a failing case
                print(f"FAIL {name} ({type(exc).__name__}: {exc})")
                n_fail += 1
                continue
        if ok:
            print(f"PASS {name}")
            n_pass += 1
        else:
            print(f"FAIL {name}")
            n_fail += 1
    ran = n_pass + n_fail
    if n_fail:
        print(f"{PROG}: self-test FAIL -- {n_fail} of {ran} cases failed")
        return 1
    print(f"{PROG}: self-test PASS -- {n_pass} of {ran} cases passed" + (f", {n_skip} skipped" if n_skip else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
