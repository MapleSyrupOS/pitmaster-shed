#!/usr/bin/env python3
"""Check STATUS line count and file citations. Python 3.8+, standard library only.

Usage: status-check.py [--root DIR] [--ceiling N] [--sections LIST]
       status-check.py --self-test

The ceiling comes from --ceiling, then pitmaster.md's status_ceiling, then 300.
The status file is STATUS.md unless pitmaster.md sets status_file. Every bullet
under a checked section must cite at least one backticked path that exists
relative to the root. The checked sections come from --sections, then
pitmaster.md's status_sections, then "Now, Next, Known debt". A list is
comma-separated section names, matched exactly against level-2 headings, for
example "Goal, Landed, In progress, Waiting on owner". Flags work alone;
pitmaster.md is read when present and a flag always wins over it.
Other backticked paths may name planned files, globs or
models and are not checked. A path contains '/' or ends in a file extension.
Trailing question references (Q9 or Q9, Q11) and :line numbers are stripped.
Checks literal paths, not glob patterns; does not verify a cited file's claims.
Exit 0: green; 1: findings; 2: invalid usage, config or unreadable input.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Dict, Iterator, List, Optional, Tuple

PROG = "status-check"
DEFAULT_SECTIONS = ("Now", "Next", "Known debt")
CONFIG_KEYS = ("status_file", "status_ceiling", "status_sections")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
BULLET = re.compile(r"^(\s*)(?:[-+*]|\d+[.)])\s+(.*)$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
TICKED = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
QUESTION_SUFFIX = re.compile(r"\s+Q\d+(?:\s*,\s*Q\d+)*$")
LINE_SUFFIX = re.compile(r":\d+(?::\d+)?$")
EXTENSION = re.compile(r"\.[A-Za-z0-9][A-Za-z0-9_-]*$")


def config(root: pathlib.Path) -> Dict[str, str]:
    """Read only the supported scalar keys; commented keys are inactive."""
    path = root / "pitmaster.md"
    if not path.exists():
        return {}
    values = {}
    pattern = re.compile(r"^\s*(" + "|".join(CONFIG_KEYS) + r"):\s*(.*)$")
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = pattern.match(line)
        if match:
            try:
                values[match.group(1)] = " ".join(shlex.split(match.group(2), comments=True))
            except ValueError as exc:
                raise ValueError("pitmaster.md:{}: {}".format(number, exc))
    return values


def positive_integer(value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value) or int(value) < 1:
        raise ValueError("ceiling must be a positive integer")
    return int(value)


def section_list(value: str) -> List[str]:
    """Split a comma-separated list of section names; none may be empty."""
    names = [name.strip() for name in value.split(",")]
    if not value.strip() or any(not name for name in names):
        raise ValueError("sections must be a comma-separated list of non-empty section names")
    return names


def path_token(token: str) -> Optional[str]:
    token = QUESTION_SUFFIX.sub("", token.strip())
    token = LINE_SUFFIX.sub("", token)
    return token if "/" in token or EXTENSION.search(token) else None


def bullets(lines: List[str], sections: Tuple[str, ...] = DEFAULT_SECTIONS) -> Iterator[List[Tuple[int, str]]]:
    """Yield checked bullets with wrapped text, skipping fenced examples."""
    section = None
    pending = []
    indent = 0
    after_blank = False
    fence_char = None
    fence_size = 0
    for number, line in enumerate(lines, 1):
        fence = FENCE.match(line)
        if fence_char is not None:
            if (fence and fence.group(1)[0] == fence_char
                    and len(fence.group(1)) >= fence_size and not fence.group(2).strip()):
                fence_char = None
            continue
        if fence:
            if pending:
                yield pending
                pending = []
            fence_char, fence_size = fence.group(1)[0], len(fence.group(1))
            continue
        heading = HEADING.match(line)
        if heading:
            if pending:
                yield pending
                pending = []
            if len(heading.group(1)) <= 2:
                section = heading.group(2) if len(heading.group(1)) == 2 else None
            continue
        if section not in sections:
            continue
        bullet = BULLET.match(line)
        if bullet:
            if pending:
                yield pending
            pending = [(number, bullet.group(2))]
            indent = len(bullet.group(1).expandtabs(4))
            after_blank = False
        elif pending:
            if not line.strip():
                after_blank = True
            elif not after_blank or len(line) - len(line.lstrip()) > indent:
                pending.append((number, line))
                after_blank = False
            else:
                yield pending
                pending = []
    if pending:
        yield pending


def findings(root: pathlib.Path, path: pathlib.Path, ceiling: int,
             sections: Tuple[str, ...] = DEFAULT_SECTIONS) -> Tuple[List[str], int]:
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    if len(lines) > ceiling:
        out.append("{}: {} lines exceeds ceiling {}".format(path.name, len(lines), ceiling))
    for bullet in bullets(lines, sections):
        citations = []
        for number, text in bullet:
            for match in TICKED.finditer(text):
                token = path_token(match.group(1))
                if token is not None:
                    citations.append((number, token))
        if not citations:
            out.append("{}:{}: bullet has no backticked file citation".format(path.name, bullet[0][0]))
        elif not any((root / token).exists() for _, token in citations):
            out.append("{}:{}: no cited path exists ({})".format(
                path.name, bullet[0][0], ", ".join("`{}`".format(t) for _, t in citations)))
    return out, len(lines)


def run(root: pathlib.Path, ceiling: Optional[int] = None, sections: Optional[List[str]] = None) -> int:
    try:
        if not root.is_dir():
            raise ValueError("--root must name an existing directory")
        values = config(root)
        limit = ceiling if ceiling is not None else positive_integer(values.get("status_ceiling", "300"))
        if limit < 1:
            raise ValueError("ceiling must be a positive integer")
        if sections is None and "status_sections" in values:
            try:
                sections = section_list(values["status_sections"])
            except ValueError as exc:
                raise ValueError("status_sections: {}".format(exc))
        checked = tuple(sections) if sections is not None else DEFAULT_SECTIONS
        status_file = values.get("status_file", "STATUS.md")
        if not status_file:
            raise ValueError("status_file must not be empty")
        path = root / status_file
        if not path.exists():
            print("FAIL {}: status file is missing".format(status_file))
            return 1
        problems, count = findings(root, path, limit, checked)
    except (OSError, UnicodeError, ValueError) as exc:
        print("status-check: {}".format(exc), file=sys.stderr)
        return 2
    for problem in problems:
        print("FAIL " + problem)
    print("status-check: {} lines, ceiling {}, {} finding(s)".format(count, limit, len(problems)))
    return 1 if problems else 0


# ---------------------------------------------------------------- self-test

def _write(path: pathlib.Path, text: str) -> None:
    """Write UTF-8 text with the newlines exactly as given, on every platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _fixture(base: pathlib.Path, status: Optional[str], settings: Optional[str]) -> pathlib.Path:
    """A project root holding three real files, a STATUS.md and a pitmaster.md (each optional)."""
    root = base / "project"
    for filename in ("README.md", "src/main.py", "docs/plan.md"):
        _write(root / filename, "fixture\n")
    if status is not None:
        _write(root / "STATUS.md", status)
    if settings is not None:
        _write(root / "pitmaster.md", settings)
    return root


def _quiet_run(root: pathlib.Path, ceiling: Optional[int] = None,
               sections: Optional[List[str]] = None) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return run(root, ceiling, sections)


def _table_case(status: Optional[str], settings: str, ceiling: Optional[int], expected: int,
                sections: Optional[List[str]] = None) -> Callable[[pathlib.Path], bool]:
    def case(base: pathlib.Path) -> bool:
        return _quiet_run(_fixture(base, status, settings), ceiling, sections) == expected
    return case


def _script_run(script: pathlib.Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([sys.executable, str(script)] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, encoding="utf-8")


def _case_configured_status_file(base: pathlib.Path) -> bool:
    root = base / "custom"
    _write(root / "pitmaster.md", 'status_file: "live state.md"\n')
    _write(root / "live state.md", "## Now\n- `pitmaster.md`.\n")
    return _quiet_run(root) == 0


def _case_flags_alone(base: pathlib.Path) -> bool:
    """No pitmaster.md at all: the flags still work."""
    root = _fixture(base, "## Goal\n- Trust me.\n## Now\n- `README.md`.\n", None)
    return (not (root / "pitmaster.md").exists()
            and _quiet_run(root, 5, ["Goal"]) == 1
            and _quiet_run(root, 5, ["Now"]) == 0
            and _quiet_run(root, 5) == 0)


def _case_crlf(base: pathlib.Path) -> bool:
    root = _fixture(base, "## Now\r\n- Fix\r\n  `docs/plan.md`.\r\n- `README.md`.\r\n", "")
    return _quiet_run(root) == 0


def _case_output_lines(base: pathlib.Path) -> bool:
    """The finding line and the summary line keep their wording (callers read them)."""
    root = _fixture(base, "## Now\n- Trust me.\n", "")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = run(root)
    return (code == 1 and out.getvalue().splitlines() == [
        "FAIL STATUS.md:2: bullet has no backticked file citation",
        "status-check: 2 lines, ceiling 300, 1 finding(s)"])


_GOAL_UNCITED = "## Goal\n- Trust me.\n## Now\n- Trust me too.\n"
_WIDE = "status_sections: Goal, Landed, In progress, Waiting on owner\n"


def _case_sections_configured_red(base: pathlib.Path) -> bool:
    """red: an uncited bullet under a configured section fails."""
    return _quiet_run(_fixture(base, "## Goal\n- Trust me.\n", _WIDE)) == 1


def _case_sections_unconfigured_passes(base: pathlib.Path) -> bool:
    """The same bullet under a section the config does not name passes."""
    return _quiet_run(_fixture(base, "## Now\n- Trust me.\n", _WIDE)) == 0


def _case_sections_default_when_unset(base: pathlib.Path) -> bool:
    """Without a setting the sections stay Now, Next and Known debt."""
    return (_quiet_run(_fixture(base, "## Goal\n- Trust me.\n", "")) == 0
            and _quiet_run(_fixture(base, "## Next\n- Trust me.\n", "")) == 1)


def _case_sections_multiword(base: pathlib.Path) -> bool:
    root = _fixture(base, "## In progress\n- Trust me.\n## Waiting on owner\n- `README.md`.\n", _WIDE)
    return _quiet_run(root) == 1 and _quiet_run(_fixture(base, "## Waiting on owner\n- `README.md`.\n", _WIDE)) == 0


def _case_sections_flag_overrides(base: pathlib.Path) -> bool:
    """The flag wins over pitmaster.md in both directions."""
    status = "## Goal\n- Trust me.\n## Now\n- `README.md`.\n"
    root = _fixture(base, status, "status_sections: Goal\n")
    return _quiet_run(root) == 1 and _quiet_run(root, None, ["Now"]) == 0


def _case_sections_flag_through_cli(base: pathlib.Path) -> bool:
    """The real command line: --sections parsed, split on commas, and winning over the file."""
    root = _fixture(base, "## Goal\n- Trust me.\n## Now\n- `README.md`.\n", "status_sections: Now\n")
    script = pathlib.Path(__file__).resolve()
    plain = _script_run(script, "--root", str(root))
    wide = _script_run(script, "--root", str(root), "--sections", "Goal, Now")
    bad = _script_run(script, "--root", str(root), "--sections", "Goal,,Now")
    return (plain.returncode == 0 and wide.returncode == 1
            and "STATUS.md:2: bullet has no backticked file citation" in wide.stdout
            and bad.returncode == 2)


def _case_sections_config_comment_inactive(base: pathlib.Path) -> bool:
    root = _fixture(base, "## Goal\n- Trust me.\n", "# status_sections: Goal\n")
    return _quiet_run(root) == 0


def _case_sections_empty_config(base: pathlib.Path) -> bool:
    return _quiet_run(_fixture(base, "## Now\n- `README.md`.\n", "status_sections:\n")) == 2


def _case_sections_blank_item_config(base: pathlib.Path) -> bool:
    return _quiet_run(_fixture(base, "## Now\n- `README.md`.\n", "status_sections: Now,, Next\n")) == 2


def _case_sections_empty_flag(base: pathlib.Path) -> bool:
    script = pathlib.Path(__file__).resolve()
    root = _fixture(base, "## Now\n- `README.md`.\n", "")
    return _script_run(script, "--root", str(root), "--sections", "").returncode == 2


def _case_copy_one_folder(base: pathlib.Path) -> bool:
    """The script works from a lone copy of its folder, with no repository and no other file."""
    solo = base / "solo"
    solo.mkdir()
    shutil.copy(str(pathlib.Path(__file__).resolve()), str(solo / "status-check.py"))
    root = _fixture(base, "## Goal\n- Trust me.\n## Now\n- `README.md`.\n", "status_sections: Now\n")
    script = solo / "status-check.py"
    return (_script_run(script, "--root", str(root)).returncode == 0
            and _script_run(script, "--root", str(root), "--sections", "Goal").returncode == 1)


# (name, status, settings, --ceiling, expected exit) -- the original arms
_TABLE = [
    ("file and directory citations", "## Now\n- Ready: `src/main.py`, `docs/`.\n", "", None, 0),
    ("wrapped citation", "## Next\n- Build the next part\n  from `docs/plan.md`.\n", "", None, 0),
    ("question and line suffixes", "## Now\n- Proof: `docs/plan.md Q9`, `src/main.py:12`, "
     "`docs/plan.md:3 Q9, Q11`.\n", "", None, 0),
    ("ignore non-path tokens", "## Now\n- `LANDED`, `abcdef1`; proof: `README.md`.\n", "", None, 0),
    ("red: no citation", "## Now\n- Trust me.\n", "", None, 1),
    ("red: non-path tokens alone", "## Next\n- `LANDED`, `abcdef1`.\n", "", None, 1),
    ("red: missing file", "## Known debt\n- Fix `docs/missing.md`.\n", "", None, 1),
    ("one real and one planned file", "## Next\n- Write `docs/new.md` from `README.md`.\n", "", None, 0),
    ("red: missing wrapped citation", "## Next\n- Fix it\n  in `src/missing.py`.\n", "", None, 1),
    ("red: wildcard is not a file", "## Next\n- Run `docs/*.md`.\n", "", None, 1),
    ("all bullet markers", "## Known debt\n* Fix `README.md`.\n+ Fix `README.md`.\n"
     "1. Fix `README.md`.\n  - Fix `docs/plan.md`.\n", "", None, 0),
    ("subheadings remain checked", "## Now\n### Details\n- No proof.\n", "", None, 1),
    ("other sections ignored", "## Log\n- No file here.\n## Now\n- `README.md`.\n"
     "## Notes\n- No file here either.\n", "", None, 0),
    ("fenced examples ignored", "## Now\n```markdown\n- No proof.\n## Next\n```\n"
     "- Actual proof `README.md`.\n", "", None, 0),
    ("red: later paragraph cannot lend citation", "## Next\n- No proof.\n\n"
     "Unrelated `README.md`.\n", "", None, 1),
    ("indented paragraph citation", "## Next\n- Planned work.\n\n  See `docs/plan.md`.\n", "", None, 0),
    ("ceiling boundary", "## Now\n- `README.md`.\n", "status_ceiling: 2\n", None, 0),
    ("red: configured ceiling", "## Now\n- `README.md`.\n", "status_ceiling: 1\n", None, 1),
    ("CLI ceiling overrides config", "## Now\n- `README.md`.\n", "status_ceiling: 1\n", 2, 0),
    ("comments and quotes", "## Now\n- `README.md`.\n", "# status_ceiling: 1\n"
     "status_ceiling: '2' # agreed\n", None, 0),
    ("red: default ceiling", "\n" * 301, "", None, 1),
    ("usage: invalid ceiling config", "", "status_ceiling: many\n", None, 2),
    ("usage: zero ceiling config", "", "status_ceiling: 0\n", None, 2),
    ("usage: unclosed config quote", "", "status_ceiling: '3\n", None, 2),
    ("missing STATUS", None, "", None, 1),
]

_CASES: List[Tuple[str, Callable[[pathlib.Path], bool]]] = (
    [(row[0], _table_case(*row[1:])) for row in _TABLE]
    + [
        ("configured status_file", _case_configured_status_file),
        ("flags work with no pitmaster.md", _case_flags_alone),
        ("CRLF status file", _case_crlf),
        ("output lines unchanged", _case_output_lines),
        ("red: uncited bullet under a configured section", _case_sections_configured_red),
        ("same bullet under an unconfigured section passes", _case_sections_unconfigured_passes),
        ("default sections when status_sections is unset", _case_sections_default_when_unset),
        ("multi-word configured sections", _case_sections_multiword),
        ("--sections overrides status_sections", _case_sections_flag_overrides),
        ("--sections through the command line", _case_sections_flag_through_cli),
        ("commented status_sections is inactive", _case_sections_config_comment_inactive),
        ("usage: empty status_sections config", _case_sections_empty_config),
        ("usage: blank item in status_sections config", _case_sections_blank_item_config),
        ("usage: empty --sections", _case_sections_empty_flag),
        ("copy-one-folder", _case_copy_one_folder),
    ]
)


def self_test() -> int:
    passed = failed = 0
    for name, case in _CASES:
        with tempfile.TemporaryDirectory(prefix="status-check-") as tmp:
            try:
                ok = case(pathlib.Path(tmp))
                detail = ""
            except Exception as exc:  # a crashing case is a failing case
                ok = False
                detail = " ({}: {})".format(type(exc).__name__, exc)
        print("{} {}{}".format("PASS" if ok else "FAIL", name, detail))
        passed += ok
        failed += not ok
    ran = passed + failed
    if failed:
        print("{}: self-test FAIL -- {} of {} cases failed".format(PROG, failed, ran))
        return 1
    print("{}: self-test PASS -- {} of {} cases passed".format(PROG, passed, ran))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".", help="project root (default: current directory)")
    parser.add_argument("--ceiling", help="positive STATUS line ceiling; overrides pitmaster.md")
    parser.add_argument("--sections", help="comma-separated sections to check, e.g. \"Goal, Landed\"; "
                        "overrides pitmaster.md (default: Now, Next, Known debt)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    ceiling = None
    sections = None
    if args.ceiling is not None:
        try:
            ceiling = positive_integer(args.ceiling)
        except ValueError as exc:
            parser.error(str(exc))
    if args.sections is not None:
        try:
            sections = section_list(args.sections)
        except ValueError as exc:
            parser.error(str(exc))
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.self_test:
        return self_test()
    return run(pathlib.Path(args.root), ceiling, sections)


if __name__ == "__main__":
    sys.exit(main())
