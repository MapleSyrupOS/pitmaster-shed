# diskscan

Finds where a machine's disk space went: the biggest folders, what grew since the last scan, and big folders
nobody has named. It is built for machines where agents work: a clone per job, a worktree per subagent, model
downloads and VM disks add up to hundreds of gigabytes in days. It only reads, and only inside a scope its owner
has read and approved.

Python 3.8 or newer, standard library only; `git` on the path for `trees`. Runs on macOS, Linux and Windows.

## Usage

```
python3 diskscan.py init [--scope FILE]       write a starting scope (never overwrites one)
python3 diskscan.py plan [--scope FILE]       show what a scan would read and write; reads nothing
python3 diskscan.py approve [--scope FILE]    record the owner's approval of the scope
python3 diskscan.py scan [--scope FILE] [--top N] [--tsv] [--alert-free SIZE] [--alert-growth SIZE]
python3 diskscan.py trees DIR... [--hours N]  read-only verdicts on the git trees in each DIR
python3 diskscan.py --self-test [--break NAME]
```

The scope lives at `~/.config/pitmaster/diskcheck/scope.txt` (`%APPDATA%\pitmaster\diskcheck\scope.txt` on
Windows) unless `--scope` names another file.

## The scope: what it may read

A plain text file, one line each:

| Line | Meaning |
|------|---------|
| `root <path> [depth N]` | A folder to measure. Folder sizes are reported N levels below it (default 1). |
| `exclude <path>` | Never read, nor anything inside it. |
| `limit <seconds>` | Stop reading after this long and mark the scan partial (default 900). |
| `ledger <path>` | Where results are appended (default: `ledger.tsv` beside the scope). |
| `big <size>` | Flag a folder this big that no `space` line names (default `5G`). |
| `space <path> <kind> [note]` | A known space. Kind: `trees` (git clones or worktrees), `cold` (keep, but could move), `cache` (an app can rebuild it), `live` (in use; leave it). |
| `approved <date> <digest>` | Written by `approve`, never by hand. |

`init` writes `root ~ depth 2`, excludes the private places it finds (mail, messages, browser profiles, keychains,
cloud drives, the photo library, `.ssh`), and adds a `space` line for each common agent space it finds.

**Approval.** The access lines are `root`, `exclude`, `limit` and `ledger`. `approve` stores a digest of them, and
`scan` refuses to run when they no longer match it. Widening what the tool reads therefore always takes the owner's
fresh approval. `space` and `big` lines describe and grant nothing, so they can change freely.

## What it reads, writes and never does

- **Reads** the names, sizes, dates and device numbers of files and folders. Never a file's contents.
- **Writes** rows to the ledger (date, path, bytes, newest date inside) and one audit line per scan: the roots,
  how many entries it read, how many folders the system refused, how many were excluded, how long it took.
- **Never** follows a symbolic link, crosses onto another disk below a root, deletes or moves anything, asks for
  raised privileges, or uses the network. A folder the system refuses is counted and named, not retried.

## What scan reports

Each disk's free space; the folders the system refused; the biggest folders, marked `[unnamed]` when no `space`
line covers them; what changed by 1 GB or more since the last scan, with a rate per day; the big folders no `space`
line names; and the size of each known space.

**Alerts, for a scheduled run.** `--alert-free 50G` raises an `ALERT free space` line for each scanned disk with less
than 50 GB free; `--alert-growth 20G` raises an `ALERT growth` line for each folder that grew 20 GB or more since the
previous scan. Alert lines print before the report, and the exit status is 3 when there is any, so a scheduler can
notify on that alone. An alerting scan reads and writes exactly what a plain scan does.

## trees

For each git tree inside each DIR, one verdict. It reads through git with `GIT_OPTIONAL_LOCKS=0`, so reading changes
nothing (a plain `git status` updates the index, which makes every tree look recently used).

| Verdict | Meaning |
|---------|---------|
| `removable` | Idle for `--hours` (default 24), not in use, nothing borrows from it, and everything in it is on a remote. |
| `in-use` | A process is working inside it. |
| `borrowed` | Another tree borrows its objects (`git clone --shared`); removing it would break that tree. |
| `source-gone` | The tree it borrowed objects from is gone; git can no longer read its history. |
| `recent` | Something outside `.git`, `build` and `node_modules` changed within `--hours`. |
| `unsaved` | It has uncommitted or untracked changes. |
| `unpushed` | Its HEAD is on no remote-tracking branch. |
| `unreadable` | git could not read it. |

It never removes anything. Removing, or saving a tree's work first, is for the person or the skill acting on their
word.

## Self-test

`--self-test` builds a throwaway home folder with a link, an excluded folder, a refused folder and a fake second
disk, plus throwaway git trees, and checks each guard. `--break NAME` switches one guard off to show its case go
red: `symlink`, `device`, `exclude`, `approve`, `denied`, `growth`, `unnamed`, `lossless`, `borrowed`, `alert`.
