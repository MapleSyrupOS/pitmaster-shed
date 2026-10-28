#!/usr/bin/env python3
"""looseends-scan.py: list a project's loose ends from the markers its records already use, for looseends.

It reads the files git tracks (every file under ROOT outside a git checkout) and reports, with file:line, each
item a marker finds, sorted into looseends' four kinds:

  question  asked and never answered: a record's Owed list, an ADR's asks left open, a proposal's open questions
  promise   said and not done: the debt list, an unticked "- [ ]" item
  ruling    waiting on the user: the status file's waiting section, calls open to veto, an unratified ADR, a
            commented entry in a publish list
  unlanded  work never landed: uncommitted files, a stash, a branch never pushed or ahead of its upstream, a
            remote branch not merged into the default branch

The markers are plain text, one per line (DEFAULT_MARKERS below, or --print-markers), so a project can write its
own file and pass it with --markers. Each line is "<type> <kind> <files> <pattern>":

  section  every top-level list item under a heading that starts with <pattern>, or the section's first line when
           it has no list; a section whose first line says Empty, None or Nothing has no items
  line     every line matching the regex <pattern>
  group    lines matching the regex <pattern>, counted as one item per file
  list     every top-level list item in the file, and each section with no list (<pattern> is "-")
  skip     files matching <files> are never read (<kind> and <pattern> are "-")
  closed   a file with a line matching the regex <pattern> is settled, so none of its items count (<kind> is "-")

<files> is a glob from ROOT: "*" and "?" stay inside one folder, "**" as a whole segment crosses folders.

Usage:
  python3 looseends-scan.py [--root DIR] [--topic TEXT]... [--markers FILE] [--no-git] [--tsv]
  python3 looseends-scan.py --print-markers
  python3 looseends-scan.py --self-test [--break empty|scope|skip|closed|group|topic]

--topic keeps only the items that mention TEXT (any case) or sit in a file whose path does; "ADR-12" and
"ADR-012" match each other. Given more than once, an item matching any of them is kept.

The guards, each named when it fails:
  empty   a section that says Empty, None or Nothing has no items
  scope   a section ends at the next heading of its level or higher
  skip    skipped files (templates, examples, the skills themselves) are never read
  closed  a settled file's items do not count
  group   calls open to veto count once per file, not once per line
  topic   --topic drops the items that do not mention the topic

The self-test builds a throwaway repository with planted loose ends of every kind and a decoy for each guard, and
proves each guard goes red on its own failure. --break NAME switches that guard off.

Output is written as UTF-8 wherever the console's default is narrower, so a project's own text cannot crash it.
This script only reads. It does not fetch, push or use the network, so remote branches are as current as the last
fetch, and open pull requests are not its to see. A branch merged by squash still shows as not merged.
Exit status: 0 nothing found, 1 loose ends found, 2 usage error. Python 3.8+, standard library only.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tempfile
from typing import Dict, List, NamedTuple, Optional, Tuple

GUARDS: Dict[str, bool] = {"empty": True, "scope": True, "skip": True, "closed": True, "group": True,
                           "topic": True}

KINDS = ("question", "promise", "ruling", "unlanded")
TYPES = ("section", "line", "group", "list", "skip", "closed")

DEFAULT_MARKERS = r"""# type   kind      files                     pattern
skip     -         skills/**                 -
skip     -         drafts/**                 -
skip     -         templates/**              -
skip     -         examples/**               -
skip     -         **/TEMPLATE.md            -
skip     -         docs/decisions/public/**  -
closed   -         docs/proposals/**         ^\*\*Status:\*\*\s*(accepted|superseded|withdrawn|retired|closed)
section  ruling    **/STATUS.md              Waiting on
section  question  **/*.md                   Owed
section  question  docs/decisions/*.md       Asks left open
section  question  docs/decisions/*.md       Open questions
section  question  docs/proposals/**         Open questions
line     ruling    docs/decisions/*.md       ^status:\s*proposed\s*$
line     ruling    publish.txt               ^#\s*[\w.*/-]+\.\w+(\s+->\s+\S+/)?\s*$
group    ruling    **/*.md                   \bveto:\s+(?!none expected|\|)\S
list     promise   **/STATUS-debt.md         -
line     promise   **/*.md                   ^\s*[-*] \[ \]
"""

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.*)")
FENCE = re.compile(r"^\s*(```|~~~)")
EMPTY = re.compile(r"(?i)^(empty|none|nothing)\b")


class Marker(NamedTuple):
    type: str
    kind: str
    files: re.Pattern
    pattern: str
    regex: Optional[re.Pattern]


class Item(NamedTuple):
    kind: str
    path: str
    line: int
    text: str
    context: str


def glob_regex(glob: str) -> re.Pattern:
    """A glob over a whole path from ROOT; "*" and "?" never cross a "/", "**" crosses any number of folders."""
    segs = glob.strip().strip("/").split("/")
    out = []
    for i, seg in enumerate(segs):
        last = i == len(segs) - 1
        if seg == "**":
            out.append(".*" if last else "(?:[^/]+/)*")
            continue
        s = re.escape(seg).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
        out.append(s if last else s + "/")
    return re.compile("^" + "".join(out) + "$")


def parse_markers(text: str) -> List[Marker]:
    markers = []
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split(None, 3)
        if len(parts) != 4 or parts[0] not in TYPES or (parts[1] not in KINDS and parts[1] != "-"):
            raise ValueError(f"marker line {n}: want '<type> <kind> <files> <pattern>', got: {raw.strip()}")
        mtype, kind, files, pattern = parts[0], parts[1], parts[2], parts[3].strip()
        if mtype not in ("skip", "closed") and kind == "-":
            raise ValueError(f"marker line {n}: a {mtype} marker needs a kind ({', '.join(KINDS)})")
        regex = None
        if mtype in ("line", "group", "closed"):
            try:
                regex = re.compile(pattern, re.MULTILINE)  # "closed" searches a whole file, so ^ is per line
            except re.error as e:
                raise ValueError(f"marker line {n}: bad regex {pattern!r}: {e}")
        markers.append(Marker(mtype, kind, glob_regex(files), pattern, regex))
    return markers


def run_git(root: pathlib.Path, *args: str) -> Optional[str]:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def is_repo(root: pathlib.Path) -> bool:
    return (run_git(root, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def project_files(root: pathlib.Path) -> List[str]:
    if is_repo(root):
        out = run_git(root, "ls-files", "-z") or ""
        return sorted(p for p in out.split("\0") if p)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(root).parts)


def clean(text: str) -> str:
    m = LIST_ITEM.match(text.strip())
    text = m.group(1) if m else text.strip()
    text = re.sub(r"\s+", " ", text.replace("**", ""))
    return text if len(text) <= 110 else text[:109] + "…"


def outline(lines: List[str]) -> List[Tuple[int, int, str]]:
    """Each heading outside a code fence, as (index, level, text)."""
    found, fenced = [], False
    for i, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
            continue
        m = None if fenced else HEADING.match(line)
        if m:
            found.append((i, len(m.group(1)), m.group(2)))
    return found


def section_end(heads: List[Tuple[int, int, str]], start: int, level: int, total: int) -> int:
    if not GUARDS["scope"]:  # the self-test's broken arm: a section runs on to the end of the file
        return total
    return next((i for i, lv, _ in heads if i > start and lv <= level), total)


def list_items(lines: List[str], lo: int, hi: int) -> List[Tuple[int, str]]:
    """Top-level list items in lines[lo:hi], each with its indented continuation as context."""
    items: List[Tuple[int, str]] = []
    fenced = False
    for i in range(lo, hi):
        line = lines[i]
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if LIST_ITEM.match(line):
            items.append((i, line))
        elif items and line.startswith((" ", "\t")) and line.strip():
            j, ctx = items[-1]
            items[-1] = (j, ctx + "\n" + line)
    return items


def section_items(lines: List[str], heads: List[Tuple[int, int, str]], start: int, level: int
                  ) -> List[Tuple[int, str]]:
    end = section_end(heads, start, level, len(lines))
    body = [(i, lines[i]) for i in range(start + 1, end) if lines[i].strip()]
    if not body:
        return []
    if GUARDS["empty"] and EMPTY.match(clean(body[0][1])):
        return []
    items = list_items(lines, start + 1, end)
    if items:
        return items
    first = next(((i, t) for i, t in body if not HEADING.match(t)), None)
    return [first] if first else []


def scan_file(rel: str, text: str, markers: List[Marker]) -> List[Item]:
    lines = text.splitlines()
    heads = outline(lines)
    items: List[Item] = []
    for m in markers:
        if m.type in ("skip", "closed") or not m.files.match(rel):
            continue
        if m.type == "section":
            for i, lv, title in heads:
                if title.lower().startswith(m.pattern.lower()):
                    for j, ctx in section_items(lines, heads, i, lv):
                        items.append(Item(m.kind, rel, j + 1, clean(ctx.splitlines()[0]), ctx))
        elif m.type == "line":
            for j, line in enumerate(lines):
                if m.regex.search(line):
                    items.append(Item(m.kind, rel, j + 1, clean(line), line))
        elif m.type == "group":
            hits = [(j, line) for j, line in enumerate(lines) if m.regex.search(line)]
            if not GUARDS["group"]:  # the self-test's broken arm: one item per line
                items.extend(Item(m.kind, rel, j + 1, clean(line), line) for j, line in hits)
            elif hits:
                j, first = hits[0]
                items.append(Item(m.kind, rel, j + 1, f"{len(hits)} matching line{'s' if len(hits) > 1 else ''}, first: {clean(first)}",
                                  "\n".join(line for _, line in hits)))
        elif m.type == "list":
            for j, ctx in list_items(lines, 0, len(lines)):
                items.append(Item(m.kind, rel, j + 1, clean(ctx.splitlines()[0]), ctx))
            for i, lv, title in heads:
                if lv >= 2 and not list_items(lines, i + 1, section_end(heads, i, lv, len(lines))):
                    items.append(Item(m.kind, rel, i + 1, clean(title), title))
    return items


def default_branch(root: pathlib.Path) -> Optional[str]:
    head = (run_git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD") or "").strip()
    if head:
        return head
    for ref in ("origin/main", "origin/master", "main", "master"):
        if run_git(root, "rev-parse", "--verify", "--quiet", ref) is not None:
            return ref
    return None


def ahead(root: pathlib.Path, base: str, ref: str) -> int:
    out = run_git(root, "rev-list", "--count", f"{base}..{ref}")
    return int(out.strip()) if out and out.strip().isdigit() else 0


def git_items(root: pathlib.Path) -> List[Item]:
    items: List[Item] = []
    status = [l for l in (run_git(root, "status", "--porcelain") or "").splitlines() if l.strip()]
    if status:
        names = [l[3:] for l in status]
        shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
        items.append(Item("unlanded", "(git)", 0, f"{len(names)} uncommitted or untracked files: {shown}",
                          "\n".join(names)))
    for line in (run_git(root, "stash", "list") or "").splitlines():
        items.append(Item("unlanded", "(git)", 0, f"stash: {clean(line)}", line))
    base = default_branch(root)
    refs = run_git(root, "for-each-ref", "--format=%(refname:short)\t%(upstream:short)\t%(upstream:track)",
                   "refs/heads") or ""
    for row in refs.splitlines():
        name, upstream, track = (row.split("\t") + ["", ""])[:3]
        if upstream:
            m = re.search(r"ahead (\d+)", track)
            if m:
                items.append(Item("unlanded", "(git)", 0,
                                  f"branch {name}: {m.group(1)} commits not pushed to {upstream}", row))
        elif base and name != base:
            n = ahead(root, base, name)
            if n:
                items.append(Item("unlanded", "(git)", 0, f"branch {name}: never pushed, {n} commits not on {base}",
                                  row))
    if base and base.startswith("origin/"):
        remotes = run_git(root, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin") or ""
        for ref in remotes.split():
            if ref in (base, "origin/HEAD", "origin"):
                continue
            n = ahead(root, base, ref)
            if n:
                items.append(Item("unlanded", "(git)", 0, f"remote branch {ref}: {n} commits not merged into {base}",
                                  ref))
    return items


def topic_regex(topic: str) -> re.Pattern:
    m = re.fullmatch(r"(?i)adr-?0*(\d+)", topic.strip())
    if m:
        return re.compile(rf"(?i)\bADR-?0*{m.group(1)}\b")
    return re.compile(re.escape(topic.strip()), re.I)


def narrow(items: List[Item], topics: List[str]) -> List[Item]:
    if not topics or not GUARDS["topic"]:  # the self-test's broken arm: every item stays
        return items
    rxs = [topic_regex(t) for t in topics]
    return [it for it in items if any(rx.search(it.context) or rx.search(it.path) for rx in rxs)]


def scan(root: pathlib.Path, markers: List[Marker], topics: List[str], use_git: bool) -> List[Item]:
    skips = [m.files for m in markers if m.type == "skip"]
    closers = [m for m in markers if m.type == "closed"]
    items: List[Item] = []
    for rel in project_files(root):
        if GUARDS["skip"] and any(rx.match(rel) for rx in skips):
            continue
        if not any(m.files.match(rel) for m in markers if m.type not in ("skip", "closed")):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if GUARDS["closed"] and any(m.files.match(rel) and m.regex.search(text) for m in closers):
            continue
        items.extend(scan_file(rel, text, markers))
    if use_git and is_repo(root):
        items.extend(git_items(root))
    seen, unique = set(), []
    for it in items:  # a line two markers find (a waiting item that is also open to veto) is listed once
        if it.path == "(git)" or (it.path, it.line) not in seen:
            seen.add((it.path, it.line))
            unique.append(it)
    return narrow(unique, topics)


def report(items: List[Item], root: pathlib.Path, tsv: bool) -> None:
    if tsv:
        for it in items:
            print(f"{it.kind}\t{it.path}:{it.line}\t{it.text}")
        return
    counts = ", ".join(f"{sum(it.kind == k for it in items)} {k}" for k in KINDS)
    print(f"looseends-scan: {len(items)} loose ends in {root} ({counts})")
    for kind in KINDS:
        group = [it for it in items if it.kind == kind]
        if group:
            print(f"\n{kind}")
            for it in group:
                where = it.path if it.path == "(git)" else f"{it.path}:{it.line}"
                print(f"  {where}  {it.text}")


def self_test(broken: Optional[str]) -> int:
    if broken:
        if broken not in GUARDS:
            print(f"looseends-scan: --break takes one of {', '.join(GUARDS)}", file=sys.stderr)
            return 2
        GUARDS[broken] = False
    failed: List[str] = []

    def check(label: str, ok: bool) -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {label}")
        if not ok:
            failed.append(label)

    files = {
        "STATUS.md": "# STATUS\n## In progress\n- Building the thing.\n## Waiting on owner\n- Rule the widget name.\n"
                     "- Approve ADR-002's public copy.\n## Landed\n- Shipped the gadget.\n",
        "docs/bbq/2026-01-01-a.md": "# bbq a\n## Owed\n- Ask about the cache size (ADR-001).\n## Defaulted\n"
                                    "| call | why | veto |\n|---|---|---|\n| Use red | taste | veto: blue |\n"
                                    "| Use tabs | habit | veto: spaces |\n| Keep it | plain | veto: none expected |\n"
                                    "| call | default | veto: |\n"
                                    "Each default gets a `veto:` line.\n",
        "docs/bbq/2026-01-02-b.md": "# bbq b\n## Owed\n\nEmpty. Every owed item was asked.\n",
        "docs/decisions/ADR-001-cache.md": "---\nstatus: proposed\n---\n# ADR-001\n## Asks left open\n"
                                           "1. The cache's eviction rule.\n",
        "docs/decisions/TEMPLATE.md": "---\nstatus: proposed\n---\n# ADR-NNN\n## Asks left open\n1. Fill me in.\n",
        "docs/proposals/open-one.md": "# Open one\n**Status:** open\n## Open questions\n1. Which colour?\n"
                                      "2. Which font?\n",
        "docs/proposals/done-one.md": "# Done one\n**Status:** accepted 2026-01-01\n## Open questions\n"
                                      "1. Settled long ago.\n",
        "publish.txt": "# publish.txt: the list. A comment that is prose.\nREADME.md\n"
                       "# docs/decisions/public/ADR-001-cache.md -> docs/decisions/\n",
        "docs/STATUS-debt.md": "# debt\n- A checker script is owed.\n## Stale facts\nFacts go stale.\n",
        "docs/looseends/2026-01-03-chat.md": "# chat\n- [ ] Push the branch.\n- [x] Answer the question.\n",
        "README.md": "hello\n",
    }
    crlf = b"# Sub\r\n## Waiting on owner\r\n- A Windows-style item.\r\n## Landed\r\n- Not this one.\r\n"
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "project"
        for rel, body in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body, encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "STATUS.md").write_bytes(crlf)  # bytes, so no newline translation on any platform
        git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=test@example.com",
               "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        subprocess.run(git + ["add", "."], check=True)
        subprocess.run(git + ["commit", "-qm", "init"], check=True)
        subprocess.run(git + ["checkout", "-qb", "feature"], check=True)
        (root / "feature.md").write_text("new\n")
        subprocess.run(git + ["add", "feature.md"], check=True)
        subprocess.run(git + ["commit", "-qm", "feature"], check=True)
        subprocess.run(git + ["checkout", "-q", "main"], check=True)
        (root / "README.md").write_text("hello, changed\n")
        subprocess.run(git + ["stash", "-q"], check=True)
        (root / "notes.md").write_text("untracked\n")

        markers = parse_markers(DEFAULT_MARKERS)
        items = scan(root, markers, [], True)

        def texts(kind: str, path: str) -> List[str]:
            return [it.text for it in items if it.kind == kind and it.path == path]

        check("green: the status file's waiting section gives its two items",
              texts("ruling", "STATUS.md") == ["Rule the widget name.", "Approve ADR-002's public copy."])
        check("green: an Owed list item is a question",
              texts("question", "docs/bbq/2026-01-01-a.md") == ["Ask about the cache size (ADR-001)."])
        check("green: an unratified ADR and its open ask are found",
              texts("ruling", "docs/decisions/ADR-001-cache.md") == ["status: proposed"]
              and texts("question", "docs/decisions/ADR-001-cache.md") == ["The cache's eviction rule."])
        check("green: an open proposal's two questions are found",
              texts("question", "docs/proposals/open-one.md") == ["Which colour?", "Which font?"])
        check("green: a commented publish entry is found, a prose comment is not",
              len(texts("ruling", "publish.txt")) == 1 and "ADR-001" in texts("ruling", "publish.txt")[0])
        check("green: the debt list gives its item and its section with no list",
              texts("promise", "docs/STATUS-debt.md") == ["A checker script is owed.", "Stale facts"])
        check("green: a file with Windows line endings (CRLF) reads the same",
              texts("ruling", "sub/STATUS.md") == ["A Windows-style item."])
        check("green: an unticked item is found, a ticked one is not",
              texts("promise", "docs/looseends/2026-01-03-chat.md") == ["[ ] Push the branch."])
        unlanded = " | ".join(texts("unlanded", "(git)"))
        check("green: uncommitted files, a stash and a branch never pushed are found",
              "notes.md" in unlanded and "stash:" in unlanded and "branch feature: never pushed" in unlanded
              and "branch main" not in unlanded)
        check("red arm empty: an Owed section that says Empty has no items",
              not [it for it in items if it.path == "docs/bbq/2026-01-02-b.md"])
        check("red arm scope: the Landed section's item is not taken as waiting",
              "Shipped the gadget." not in texts("ruling", "STATUS.md"))
        check("red arm skip: the template's ask is not read",
              not [it for it in items if it.path == "docs/decisions/TEMPLATE.md"])
        check("red arm closed: an accepted proposal's question does not count",
              not [it for it in items if it.path == "docs/proposals/done-one.md"])
        vetoes = texts("ruling", "docs/bbq/2026-01-01-a.md")
        check("red arm group: two calls open to veto are one item; 'none expected', a header and prose are not",
              len(vetoes) == 1 and vetoes[0].startswith("2 matching lines"))
        narrowed = scan(root, markers, ["ADR-1"], False)
        check("red arm topic: --topic ADR-1 keeps the four ADR-001 items and not ADR-002's",
              len(narrowed) == 4 and all("ADR-002" not in it.context for it in narrowed))

    if failed:
        print(f"looseends-scan: self-test FAILED: {', '.join(failed)}")
        return 1
    print("looseends-scan: self-test passed")
    return 0


def main(argv: List[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if argv[:1] == ["--self-test"]:
        if len(argv) == 1:
            return self_test(None)
        if len(argv) == 3 and argv[1] == "--break":
            return self_test(argv[2])
        print("usage: looseends-scan.py --self-test [--break " + "|".join(GUARDS) + "]", file=sys.stderr)
        return 2
    if argv == ["--print-markers"]:
        print(DEFAULT_MARKERS, end="")
        return 0
    root, topics, marker_file, use_git, tsv = pathlib.Path("."), [], None, True, False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--root", "--topic", "--markers"):
            if i + 1 >= len(argv):
                print(f"looseends-scan: {arg} needs a value", file=sys.stderr)
                return 2
            value = argv[i + 1]
            if arg == "--root":
                root = pathlib.Path(value)
            elif arg == "--topic":
                topics.append(value)
            else:
                marker_file = pathlib.Path(value)
            i += 2
            continue
        if arg == "--no-git":
            use_git = False
        elif arg == "--tsv":
            tsv = True
        else:
            print("usage: looseends-scan.py [--root DIR] [--topic TEXT]... [--markers FILE] [--no-git] [--tsv]"
                  " | --print-markers | --self-test", file=sys.stderr)
            return 2
        i += 1
    if not root.is_dir():
        print(f"looseends-scan: {root} is not a folder", file=sys.stderr)
        return 2
    try:
        text = marker_file.read_text(encoding="utf-8") if marker_file else DEFAULT_MARKERS
        markers = parse_markers(text)
    except (OSError, ValueError) as e:
        print(f"looseends-scan: {e}", file=sys.stderr)
        return 2
    root = root.resolve()
    items = scan(root, markers, topics, use_git)
    report(items, root, tsv)
    return 1 if items else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:  # piped into head: the reader stopped early
        sys.exit(0)
