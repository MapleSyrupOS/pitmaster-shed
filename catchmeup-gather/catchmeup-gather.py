#!/usr/bin/env python3
"""catchmeup-gather.py: list a project's changes since a date or a person's last caught-up stamp.

The agent writes the explanation; this script only gathers facts and their proof. A gather reads the repository,
its log headings and (when available) pull requests through gh. It never fetches, commits or summarises.

Usage:
  python3 catchmeup-gather.py gather [--root DIR] [--since DATE] [--until DATE] [--person NAME]
                                   [--store home|commit] [--log FILE]... [--branch NAME] [--no-gh] [--json]
  python3 catchmeup-gather.py stamp --show [--root DIR] [--person NAME] [--store home|commit]
  python3 catchmeup-gather.py stamp --set [DATE|now] [--root DIR] [--person NAME] [--store home|commit]
  python3 catchmeup-gather.py --self-test [--break window|firstparent|sharedkey|logdate|logref|localdate|nostamp|store|until]

ROOT defaults to the current directory; git finds its repository top level. The person defaults to "the user".
--since wins over the stamp. Dates are YYYY-MM-DD or ISO date-times; a bare date or a date-time without a zone
means the local time zone, since "since the 22nd" is the user's own day. Stamps are always written in UTC with a
Z. The start is inclusive and the end is the current UTC time, or --until (inclusive; a bare date means the end
of that local day), which reads the stretch before a window for the catch-up's "where we left off". Without a start, gather prints
"no start point: ask the user for a date" and exits 3. It never silently reads all history.

The home store is $PITMASTER_HOME/catchmeup.json (default ~/.pitmaster/catchmeup.json), keyed by project then
person. The project is the normalised origin URL: no scheme, user@ or final .git, a lowercased host, and a slash
in place of the colon in host:path. With no origin it is the absolute main-worktree path, found through git's
common directory, so linked worktrees share stamps. A home write preserves other keys and uses temp + replace.
The commit store is docs/catchup/stamps.md, headed "# catchmeup stamps", with "- <person>: <UTC ISO date-time>"
for each person. The script writes that file; the agent commits it. --show prints the value or "none".
--set without a value, or with "now", writes the current UTC time; a date writes local midnight that day, in UTC.

Gather lists four sections:
  commits      first-parent commits on the default branch, oldest first, with short hash, committer date, subject
  pull requests  merged or open and updated in the window, with number, date, title and draft where applicable
  log headings lines starting with ## or ### and containing a date, with file:line; only the first date counts
  records      docs/ paths touched by the listed commits, sorted and deduplicated by path, with A/M/D and hashes

The branch is --branch, else origin/HEAD's target, else main, else master. A remote-tracking origin/<branch>
is preferred when present. Refs are as current as the last fetch. Record status is the last touch in the window;
renames appear as deletion plus addition, and merges are compared with their first parent. Explicit --log paths
are relative to ROOT; otherwise docs/STATUS-log.md is read when it exists. A log is read as the branch has it,
the same ref the commits come from, so a stale checkout cannot hide the window's entries; a log the branch does
not have is read from the working tree. Log dates are compared by the start's local calendar day, since a heading
has no time of day. Future headings are excluded.

--no-gh, a missing gh, or a failed gh command gives "gh: not read (<reason>)" and gathering carries on.
No network is used except gh. Its two queries are limited to 200 PRs each; local refs may be stale.
--json prints one object: start, start_source, branch, commits, prs (null when unread), log_entries, records.
In JSON mode the gh notice goes to stderr. Plain text is the default; both streams use UTF-8.
Gather and stamp --show only read. Only stamp --set creates folders or writes a stamp store.

The guards, each named when it fails:
  window       a commit a day before the start is not listed
  firstparent  a merged side-branch commit is not listed on its own; its merge is
  sharedkey    worktrees with no origin share their home stamp
  logdate      a heading before the start is excluded and one inside is included
  logref       a log is read from the branch, not the working tree (an uncommitted heading is not listed)
  localdate    a bare date is the local day's midnight, not UTC's (checked where time.tzset exists)
  nostamp      no date and no stamp exits 3 instead of listing all history
  store        each store writes only its own file, leaving the other store untouched

The self-test makes a throwaway git repository and worktree, sets a temporary PITMASTER_HOME, configures its
own git identity, plants dated commits and log headings, and uses --no-gh. --break NAME switches off that guard
to prove its check goes red. Exit status: 0 success, 2 usage/read/write error, 3 no start point; self-test 0/1.
Python 3.8+, standard library only.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

GUARDS = {"window": True, "firstparent": True, "sharedkey": True, "logdate": True, "logref": True,
          "localdate": True, "nostamp": True, "store": True, "until": True}
UTC = dt.timezone.utc
HEADING = re.compile(r"^#{2,3} .*?(\d{4}-\d{2}-\d{2})")
STAMP_LINE = re.compile(r"^- (.+): (\d{4}-\d{2}-\d{2}T.*)$")


def date_time(value: str) -> dt.datetime:
    """Read an ISO date or date-time, with the local time zone for a missing zone."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[Tt ])", value):
        raise ValueError("want YYYY-MM-DD or an ISO date-time: " + value)
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None:
            if not GUARDS["localdate"]:  # broken arm: a bare date is UTC's midnight
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone().astimezone(UTC)  # a naive astimezone() reads it as local time
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError("invalid date: " + value)


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def git(root: pathlib.Path, *args: str, optional: bool = False) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
    if result.returncode and not optional:
        raise ValueError(result.stderr.strip() or "git failed: " + " ".join(args))
    return "" if result.returncode else result.stdout


def repository(root: str) -> pathlib.Path:
    return pathlib.Path(git(pathlib.Path(root), "rev-parse", "--show-toplevel").strip()).resolve()


def project_key(root: pathlib.Path) -> str:
    origin = git(root, "remote", "get-url", "origin", optional=True).strip()
    if origin:
        if "://" in origin:
            parsed = urlsplit(origin)
            host = (parsed.hostname or "").lower()
            if ":" in host:  # keep IPv6 hosts separate from a port
                host = "[" + host + "]"
            port = ":" + str(parsed.port) if parsed.port is not None else ""
            key = host + port + "/" + parsed.path.lstrip("/")
        else:
            value = origin.rsplit("@", 1)[-1]
            match = re.match(r"^([^/:]+):(.+)$", value)
            if match:
                key = match.group(1).lower() + "/" + match.group(2).lstrip("/")
            elif "/" in value and not value.startswith(("/", ".")):
                host, path = value.split("/", 1)
                key = host.lower() + "/" + path
            else:
                key = value
        key = key.rstrip("/")
        return key[:-4] if key.endswith(".git") else key
    if not GUARDS["sharedkey"]:  # broken arm: each linked worktree gets a different key
        return str(root)
    common = pathlib.Path(git(root, "rev-parse", "--git-common-dir").strip())
    common = (root / common).resolve() if not common.is_absolute() else common.resolve()
    return str(common.parent)  # a working repository's common directory is its main .git folder


def home_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PITMASTER_HOME", str(pathlib.Path.home() / ".pitmaster"))) / "catchmeup.json"


def home_data(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
        raise ValueError("home stamp store must map project keys to people: " + str(path))
    return data


def read_stamp(root: pathlib.Path, person: str, store: str):
    if store == "home":
        value = home_data(home_path()).get(project_key(root), {}).get(person)
    else:
        path = root / "docs/catchup/stamps.md"
        value = None
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                match = STAMP_LINE.match(line)
                if match and match.group(1) == person:
                    value = match.group(2)
    if value is not None:
        if not isinstance(value, str):
            raise ValueError("stamp must be an ISO date-time")
        date_time(value)  # a damaged store must not silently turn into all history
    return value


def atomic_write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(path.parent),
                                         prefix="." + path.name + ".", delete=False) as stream:
            name = stream.name
            stream.write(text)
        os.replace(name, str(path))
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


def write_stamp(root: pathlib.Path, person: str, store: str, value: str) -> None:
    if not GUARDS["store"]:  # broken arm: the chosen store is ignored
        store = "commit" if store == "home" else "home"
    if store == "home":
        path = home_path()
        data = home_data(path)
        data.setdefault(project_key(root), {})[person] = value
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return
    path = root / "docs/catchup/stamps.md"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else ["# catchmeup stamps", ""]
    if "# catchmeup stamps" not in lines:
        raise ValueError("commit stamp store needs heading '# catchmeup stamps': " + str(path))
    replacement = "- " + person + ": " + value
    found, updated = False, []
    for line in lines:
        match = STAMP_LINE.match(line)
        if match and match.group(1) == person:
            if not found:
                updated.append(replacement)
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(replacement)
    atomic_write(path, "\n".join(updated) + "\n")


def default_branch(root: pathlib.Path, requested):
    branch = requested
    if branch is None:
        branch = git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", optional=True).strip()
    if not branch:
        branch = next((name for name in ("main", "master")
                       if git(root, "show-ref", "--verify", "refs/remotes/origin/" + name, optional=True)
                       or git(root, "show-ref", "--verify", "refs/heads/" + name, optional=True)), None)
    if not branch:
        raise ValueError("no default branch found: pass --branch NAME")
    name = branch[len("origin/"):] if branch.startswith("origin/") else branch
    for ref in ("refs/remotes/origin/" + name, "refs/heads/" + name):
        if git(root, "show-ref", "--verify", ref, optional=True):
            return ref, ref.replace("refs/remotes/", "").replace("refs/heads/", "")
    raise ValueError("branch not found: " + branch)


def commit_items(root: pathlib.Path, ref: str, start: dt.datetime, end: dt.datetime) -> list:
    args = ["log", "--reverse", "--format=%h%x00%cI%x00%s"]
    if GUARDS["firstparent"]:
        args.append("--first-parent")
    rows = git(root, *args, ref, "--").splitlines()
    items = []
    for row in rows:
        short, date, subject = row.split("\0", 2)
        when = date_time(date)
        if when <= end and (when >= start or not GUARDS["window"]):
            items.append({"hash": short, "date": date, "subject": subject})
    return items


def record_items(root: pathlib.Path, commits: list) -> list:
    records = {}
    for commit in commits:
        short = commit["hash"]
        parents = git(root, "rev-list", "--parents", "-n", "1", short).split()
        revisions = [parents[1], short] if len(parents) > 1 else ["--root", short]
        fields = git(root, "diff-tree", "--no-commit-id", "--name-status", "-r", "-z", "--no-renames",
                     *revisions, "--", "docs/").split("\0")
        for i in range(0, len(fields) - 1, 2):
            status, path = fields[i:i + 2]
            item = records.setdefault(path, {"path": path, "status": status, "commits": []})
            item["status"] = status if status in ("A", "D") else "M"
            item["commits"].append(short)
    return [records[path] for path in sorted(records)]


def log_text(root: pathlib.Path, ref: str, filename: str):
    """The log as the branch has it, else as the working tree has it, else None."""
    if GUARDS["logref"]:  # broken arm: the working tree only
        shown = subprocess.run(["git", "-C", str(root), "show", ref + ":" + filename], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        if shown.returncode == 0:
            return shown.stdout.decode("utf-8", errors="replace")
    path = root / filename
    return path.read_text(encoding="utf-8") if path.exists() else None


def log_items(root: pathlib.Path, ref: str, logs, start: dt.datetime, end: dt.datetime) -> list:
    explicit = logs is not None
    first_day = start.astimezone().date()
    items = []
    for filename in dict.fromkeys(logs if explicit else ["docs/STATUS-log.md"]):
        text = log_text(root, ref, filename)
        if text is None:
            if explicit:
                raise ValueError("log not found on the branch or in the working tree: " + filename)
            continue
        for number, line in enumerate(text.splitlines(), 1):
            match = HEADING.match(line)
            if not match:
                continue
            try:
                day = dt.date.fromisoformat(match.group(1))
            except ValueError:  # a date-shaped typo is not a calendar date
                continue
            if day <= end.astimezone().date() and (day >= first_day or not GUARDS["logdate"]):
                items.append({"file": filename, "line": number, "date": str(day), "text": line})
    return items


def pr_items(root: pathlib.Path, start: dt.datetime, end: dt.datetime, disabled: bool):
    if disabled:
        return None, "--no-gh"
    if shutil.which("gh") is None:
        return None, "gh missing"
    items = []
    try:
        for state, field in (("merged", "mergedAt"), ("open", "updatedAt")):
            search = ("merged" if state == "merged" else "updated") + ":>=" + str(start.date())
            fields = "number,title," + field + (",isDraft" if state == "open" else "")
            result = subprocess.run(["gh", "pr", "list", "--state", state, "--search", search,
                                     "--json", fields, "--limit", "200"], cwd=str(root),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    encoding="utf-8", errors="replace", timeout=60)
            if result.returncode:
                return None, " ".join(result.stderr.split()) or "gh exited " + str(result.returncode)
            for pr in json.loads(result.stdout):
                when = date_time(pr[field])
                if start <= when <= end:
                    items.append({"number": pr["number"], "date": pr[field], "title": pr["title"],
                                  "state": state, "draft": bool(pr.get("isDraft", False))})
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError) as error:
        return None, " ".join(str(error).split()) or "invalid gh response"
    return sorted(items, key=lambda pr: (date_time(pr["date"]), pr["number"])), None


def report(data: dict, reason, end: dt.datetime) -> None:
    print("catchmeup-gather: " + data["start"] + " to " + iso(end) + " (" + data["start_source"] + ")")
    print("\nCommits on " + data["branch"] + " (first-parent; refs are as current as the last fetch)")
    for item in data["commits"]:
        print("  {hash}  {date}  {subject}".format(**item))
    print("\nPull requests")
    if data["prs"] is None:
        print("gh: not read (" + reason + ")")
    else:
        for item in data["prs"]:
            print("  #{number}  {date}  {state}{draft}  {title}".format(
                **dict(item, draft=" draft" if item["draft"] else "")))
    print("\nDated log headings")
    for item in data["log_entries"]:
        print("  {file}:{line}  {text}".format(**item))
    print("\nRecord files changed")
    for item in data["records"]:
        print("  {status}  {path}  ({proof})".format(**dict(item, proof=", ".join(item["commits"]))))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = result.add_subparsers(dest="command", required=True)
    gather = commands.add_parser("gather")
    stamp = commands.add_parser("stamp")
    for command in (gather, stamp):
        command.add_argument("--root", default=".")
        command.add_argument("--person", default="the user")
        command.add_argument("--store", choices=("home", "commit"), default="home")
    gather.add_argument("--since")
    gather.add_argument("--until")
    gather.add_argument("--log", action="append")
    gather.add_argument("--branch")
    gather.add_argument("--no-gh", action="store_true")
    gather.add_argument("--json", action="store_true")
    mode = stamp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--show", action="store_true")
    mode.add_argument("--set", nargs="?", const="now", dest="set_value")
    return result


def main(argv: list) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if argv[:1] == ["--self-test"]:
        test_parser = argparse.ArgumentParser()
        test_parser.add_argument("--self-test", action="store_true")
        test_parser.add_argument("--break", choices=GUARDS, dest="broken")
        return self_test(test_parser.parse_args(argv).broken)
    args = parser().parse_args(argv)
    try:
        if not args.person.strip() or any(c in args.person for c in "\r\n\0"):
            raise ValueError("--person needs a nonempty name on one line")
        root = repository(args.root)
        end = dt.datetime.now(UTC)
        if args.command == "stamp":
            if args.show:
                print(read_stamp(root, args.person, args.store) or "none")
            else:
                value = end if args.set_value == "now" else date_time(args.set_value)
                write_stamp(root, args.person, args.store, iso(value))
                print(iso(value))
            return 0
        value = args.since if args.since is not None else read_stamp(root, args.person, args.store)
        if value is None:
            if GUARDS["nostamp"]:
                print("no start point: ask the user for a date", file=sys.stderr)
                return 3
            value = "1970-01-01"  # broken arm: silently read all history
        start = date_time(value)
        if args.until is not None and GUARDS["until"]:
            end = date_time(args.until)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.until.strip()):
                end += dt.timedelta(days=1, microseconds=-1)  # a bare date covers that whole local day
        source = "--since" if args.since is not None else "stamp (" + args.store + ")"
        ref, branch = default_branch(root, args.branch)
        commits = commit_items(root, ref, start, end)
        prs, reason = pr_items(root, start, end, args.no_gh)
        data = {"start": iso(start), "start_source": source, "branch": branch, "commits": commits,
                "prs": prs, "log_entries": log_items(root, ref, args.log, start, end),
                "records": record_items(root, commits)}
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if reason:
                print("gh: not read (" + reason + ")", file=sys.stderr)
        else:
            report(data, reason, end)
        return 0
    except (OSError, ValueError, OverflowError) as error:
        print("catchmeup-gather: " + str(error), file=sys.stderr)
        return 2


def self_test(broken) -> int:
    saved_guards = GUARDS.copy()
    saved_home = os.environ.get("PITMASTER_HOME")
    if broken:
        GUARDS[broken] = False
    failed = []

    def check(name, condition):
        print(("ok   " if condition else "FAIL ") + name)
        if not condition:
            failed.append(name)

    def invoke(root, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(args) + ["--root", str(root)])
        return rc, out.getvalue(), err.getvalue()

    try:
        with tempfile.TemporaryDirectory(prefix="catchmeup-test-") as temporary:
            base = pathlib.Path(temporary).resolve()
            root = base / "project"
            os.environ["PITMASTER_HOME"] = str(base / "home")
            root.mkdir()
            git(root, "init", "-q")
            git(root, "symbolic-ref", "HEAD", "refs/heads/main")
            git(root, "config", "user.name", "catchmeup test")
            git(root, "config", "user.email", "test@example.com")
            git(root, "config", "commit.gpgsign", "false")

            def commit(subject, day):
                git(root, "add", ".")
                env = dict(os.environ, GIT_AUTHOR_DATE=day + "T12:00:00Z", GIT_COMMITTER_DATE=day + "T12:00:00Z")
                subprocess.run(["git", "-C", str(root), "commit", "-qm", subject], env=env,
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            (root / "docs").mkdir()
            (root / "docs/record.md").write_text("old\n", encoding="utf-8")
            commit("before window", "2020-01-01")
            (root / "docs/record.md").write_text("inside\n", encoding="utf-8")
            # The old heading sits days before the start, so the check holds in any local time zone.
            (root / "docs/STATUS-log.md").write_text(
                "# log\n## 2019-12-30 old; 2020-01-03 decoy\n### 2020-01-02 inside\n"
                "## 2999-01-01 future\n", encoding="utf-8")
            commit("inside window", "2020-01-02")
            git(root, "checkout", "-qb", "side")
            (root / "docs/side.md").write_text("side\n", encoding="utf-8")
            commit("side branch only", "2020-01-03")
            git(root, "checkout", "-q", "main")
            env = dict(os.environ, GIT_AUTHOR_DATE="2020-01-04T12:00:00Z", GIT_COMMITTER_DATE="2020-01-04T12:00:00Z")
            subprocess.run(["git", "-C", str(root), "merge", "--no-ff", "-qm", "merge side", "side"],
                           env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with open(str(root / "docs/STATUS-log.md"), "a", encoding="utf-8") as stream:
                stream.write("## 2020-01-03 uncommitted\n")  # in the working tree only, never on the branch
            rc, output, _ = invoke(root, "gather", "--since", "2020-01-02T06:00:00Z", "--no-gh", "--json")
            data = json.loads(output)
            subjects = [item["subject"] for item in data["commits"]]
            check("window", rc == 0 and "before window" not in subjects and "inside window" in subjects)
            check("firstparent", "side branch only" not in subjects and "merge side" in subjects)
            check("logdate", [(entry["line"], entry["date"]) for entry in data["log_entries"]
                              if "uncommitted" not in entry["text"]] == [(3, "2020-01-02")])
            check("logref", not any("uncommitted" in entry["text"] for entry in data["log_entries"]))
            if hasattr(time, "tzset"):
                saved_tz = os.environ.get("TZ")
                os.environ["TZ"] = "ABC+5"  # five hours behind UTC, with no time-zone database needed
                time.tzset()
                try:
                    check("localdate", iso(date_time("2020-01-02")) == "2020-01-02T05:00:00Z")
                finally:
                    if saved_tz is None:
                        os.environ.pop("TZ", None)
                    else:
                        os.environ["TZ"] = saved_tz
                    time.tzset()
            else:
                print("skip localdate (no time.tzset here)")
            rc, output, _ = invoke(root, "gather", "--since", "2019-12-31T00:00:00Z", "--until", "2020-01-01T18:00:00Z",
                                "--no-gh", "--json")
            until_subjects = [item["subject"] for item in json.loads(output)["commits"]] if rc == 0 else []
            check("until", until_subjects == ["before window"])
            rc, _, error = invoke(root, "gather", "--person", "no stamp", "--no-gh")
            check("nostamp", rc == 3 and error.strip() == "no start point: ask the user for a date")

            worktree = base / "other"
            git(root, "worktree", "add", "-q", "-b", "other", str(worktree))
            # Isolate this check from a deliberately broken store guard.
            store_guard = GUARDS["store"]
            GUARDS["store"] = True
            try:
                rc, _, _ = invoke(root, "stamp", "--set", "2020-01-02T00:00:00Z", "--person", "shared")
                show_rc, value, _ = invoke(worktree, "stamp", "--show", "--person", "shared")
                gather_rc, gathered, _ = invoke(worktree, "gather", "--person", "shared", "--no-gh", "--json")
                shared_data = json.loads(gathered) if gather_rc == 0 else {}
                check("sharedkey", rc == show_rc == gather_rc == 0 and value.strip() == "2020-01-02T00:00:00Z"
                      and shared_data.get("start_source") == "stamp (home)")
            finally:
                GUARDS["store"] = store_guard

            path = home_path()
            before_home = path.read_bytes()
            stamps = root / "docs/catchup/stamps.md"
            rc, _, _ = invoke(root, "stamp", "--set", "2020-01-03T00:00:00Z", "--store", "commit", "--person", "commit person")
            commit_ok = (rc == 0 and stamps.exists() and path.read_bytes() == before_home
                         and "- commit person: 2020-01-03T00:00:00Z" in stamps.read_text(encoding="utf-8"))
            before_commit = stamps.read_bytes() if stamps.exists() else None
            rc, _, _ = invoke(root, "stamp", "--set", "2020-01-04T00:00:00Z", "--person", "home person")
            after_commit = stamps.read_bytes() if stamps.exists() else None
            home_ok = (rc == 0 and before_commit == after_commit
                       and home_data(path).get(project_key(root), {}).get("home person") == "2020-01-04T00:00:00Z")
            check("store", commit_ok and home_ok)
    finally:
        GUARDS.update(saved_guards)
        if saved_home is None:
            os.environ.pop("PITMASTER_HOME", None)
        else:
            os.environ["PITMASTER_HOME"] = saved_home
    if failed:
        print("catchmeup-gather: self-test FAILED: " + ", ".join(failed))
        return 1
    print("catchmeup-gather: self-test passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:  # piped into head: the reader stopped early
        sys.exit(0)
